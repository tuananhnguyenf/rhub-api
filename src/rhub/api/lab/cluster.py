import functools
import logging

import sqlalchemy
from connexion import problem
from flask import Response, url_for

from rhub.api import DEFAULT_PAGE_LIMIT, db, di
from rhub.api.lab.region import _user_can_access_region
from rhub.api.utils import date_now, date_parse, db_sort
from rhub.auth import model as auth_model
from rhub.auth import utils as auth_utils
from rhub.lab import SHAREDCLUSTER_GROUP, model
from rhub.lab import utils as lab_utils
from rhub.messaging import Messaging
from rhub.openstack import model as openstack_model
from rhub.tower.client import TowerError


logger = logging.getLogger(__name__)


def _user_is_cluster_admin(user_id):
    """Check if user is cluster admin."""
    user = auth_model.User.query.get(user_id)
    return user.is_admin or auth_model.Role.LAB_CLUSTER_ADMIN in user.roles


def _user_can_access_cluster(cluster, user_id):
    """Check if user can access cluster."""
    if _user_is_cluster_admin(user_id):
        return True
    if cluster.owner_id == user_id:
        return True
    if cluster.group_id is not None:
        return cluster.group_id in auth_utils.user_group_ids(user_id)
    return False


def _user_can_create_reservation(region, user_id):
    """Check if user can create in reservations in the region."""
    if _user_is_cluster_admin(user_id):
        return True
    if region.reservations_enabled:
        return True
    return region.owner_group_id in auth_utils.user_group_ids(user_id)


def _user_can_set_lifespan(region, user_id):
    """Check if user can set/change lifespan expiration of cluster in the region."""
    if _user_is_cluster_admin(user_id):
        return True
    if auth_utils.is_user_in_group(user_id, SHAREDCLUSTER_GROUP):
        return True
    return region.owner_group_id in auth_utils.user_group_ids(user_id)


def _user_can_disable_expiration(region, user_id):
    """Check if user can disable cluster reservation expiration."""
    if _user_is_cluster_admin(user_id):
        return True
    if auth_utils.is_user_in_group(user_id, SHAREDCLUSTER_GROUP):
        return True
    return region.owner_group_id in auth_utils.user_group_ids(user_id)


def _user_can_create_sharedcluster(user_id):
    if _user_is_cluster_admin(user_id):
        return True
    return auth_utils.is_user_in_group(user_id, SHAREDCLUSTER_GROUP)


@functools.lru_cache()
def _get_sharedcluster_group_id():
    q = auth_model.Group.query.filter(auth_model.Group.name == SHAREDCLUSTER_GROUP)
    if q.count():
        return q.first().id
    logger.error(f'{SHAREDCLUSTER_GROUP=} does not exist')
    return None


def _cluster_href(cluster):
    href = {
        'cluster': url_for('.rhub_api_lab_cluster_get_cluster',
                           cluster_id=cluster.id),
        'cluster_events': url_for('.rhub_api_lab_cluster_list_cluster_events',
                                  cluster_id=cluster.id),
        'cluster_hosts': url_for('.rhub_api_lab_cluster_list_cluster_hosts',
                                 cluster_id=cluster.id),
        'cluster_reboot_hosts': url_for('.rhub_api_lab_cluster_reboot_hosts',
                                        cluster_id=cluster.id),
        'region': url_for('.rhub_api_lab_region_get_region',
                          region_id=cluster.region_id),
        'product': url_for('.rhub_api_lab_product_get_product',
                           product_id=cluster.product_id),
        'owner': url_for('.rhub_api_auth_user_user_get',
                         user_id=cluster.owner_id),
        'openstack': url_for('.rhub_api_openstack_cloud_get',
                             cloud_id=cluster.region.openstack_id),
        'project': url_for('.rhub_api_openstack_project_get',
                           project_id=cluster.project_id),
    }
    if cluster.group_id:
        href['group'] = url_for('.rhub_api_auth_group_group_get',
                                group_id=cluster.group_id)
    return href


def _cluster_event_href(cluster_event):
    href = {
        'cluster': url_for('.rhub_api_lab_cluster_get_cluster',
                           cluster_id=cluster_event.cluster_id),
        'event': url_for('.rhub_api_lab_cluster_get_cluster_event',
                         event_id=cluster_event.id)
    }
    if cluster_event.user_id:
        href['user'] = url_for('.rhub_api_auth_user_user_get',
                               user_id=cluster_event.user_id)
    if (cluster_event.type == model.ClusterEventType.TOWER_JOB
            and cluster_event.tower_id and cluster_event.tower_job_id):
        href['tower'] = url_for('.rhub_api_tower_get_server',
                                server_id=cluster_event.tower_id)
        href['event_stdout'] = url_for('.rhub_api_lab_cluster_get_cluster_event_stdout',
                                       event_id=cluster_event.id)
    return href


def _cluster_host_href(cluster_host):
    href = {
        'cluster': url_for('.rhub_api_lab_cluster_get_cluster',
                           cluster_id=cluster_host.cluster_id),
    }
    return href


def list_clusters(user, filter_, sort=None, page=0, limit=DEFAULT_PAGE_LIMIT):
    if _user_is_cluster_admin(user):
        clusters = model.Cluster.query
    else:
        user_groups = auth_utils.user_group_ids(user)
        if sharedcluster_group_id := _get_sharedcluster_group_id():
            user_groups.add(sharedcluster_group_id)
        clusters = model.Cluster.query.filter(sqlalchemy.or_(
            model.Cluster.owner_id == user,
            model.Cluster.group_id.in_(user_groups),
        ))

    clusters = clusters.outerjoin(
        openstack_model.Project,
        openstack_model.Project.id == model.Cluster.project_id,
    )

    if 'name' in filter_:
        clusters = clusters.filter(model.Cluster.name.ilike(filter_['name']))

    if 'region_id' in filter_:
        clusters = clusters.filter(model.Cluster.region_id == filter_['region_id'])

    if 'owner_id' in filter_:
        clusters = clusters.filter(model.Cluster.owner_id == filter_['owner_id'])

    if 'owner_name' in filter_:
        owner = sqlalchemy.orm.aliased(auth_model.User)
        clusters = clusters.outerjoin(
            owner, owner.id == openstack_model.Project.owner_id
        )
        clusters = clusters.filter(owner.name == filter_['owner_name'])

    if 'group_id' in filter_:
        clusters = clusters.filter(model.Cluster.group_id == filter_['group_id'])

    if 'group_name' in filter_:
        group = sqlalchemy.orm.aliased(auth_model.Group)
        clusters = clusters.outerjoin(
            group, group.id == openstack_model.Project.group_id
        )
        clusters = clusters.filter(group.name == filter_['group_name'])

    if 'status' in filter_:
        clusters = clusters.filter(
            model.Cluster.status == model.ClusterStatus(filter_['status'])
        )

    if 'status_flag' in filter_:
        clusters = clusters.filter(
            model.Cluster.status.in_(
                model.ClusterStatus.flag_statuses(filter_['status_flag'])
            )
        )

    if 'shared' in filter_:
        if sharedcluster_group_id := _get_sharedcluster_group_id():
            if filter_['shared']:
                clusters = clusters.filter(
                    model.Cluster.group_id == sharedcluster_group_id
                )
            else:
                clusters = clusters.filter(
                    model.Cluster.group_id != sharedcluster_group_id
                )

    if filter_.get('deleted', False):
        clusters = clusters.filter(
            model.Cluster.status == model.ClusterStatus.DELETED
        )
    else:
        clusters = clusters.filter(
            model.Cluster.status != model.ClusterStatus.DELETED
        )

    if sort:
        clusters = db_sort(clusters, sort, {
            'name': 'lab_cluster.name',
        })

    return {
        'data': [
            cluster.to_dict() | {'_href': _cluster_href(cluster)}
            for cluster in clusters.limit(limit).offset(page * limit)
        ],
        'total': clusters.count(),
    }


def create_cluster(body, user):
    region = model.Region.query.get(body['region_id'])
    if not region:
        return problem(404, 'Not Found', f'Region {body["region_id"]} does not exist')

    if not _user_can_access_region(region, user):
        return problem(403, 'Forbidden',
                       "You don't have permissions to use selected region")

    if not region.enabled:
        return problem(403, 'Forbidden', 'Selected region is disabled')

    if not _user_can_create_reservation(region, user):
        return problem(403, 'Forbidden',
                       'Reservations are disabled in the selected region, only admin '
                       'and region owners are allowed to create new reservations.')

    query = model.Cluster.query.filter(
        db.and_(
            model.Cluster.name == body['name'],
            model.Cluster.status != model.ClusterStatus.DELETED,
        )
    )
    if query.count() > 0:
        return problem(
            400, 'Bad Request',
            f'Cluster with name {body["name"]!r} already exists',
        )

    product = model.Product.query.get(body['product_id'])
    if not product:
        return problem(404, 'Not Found', f'Product {body["product_id"]} does not exist')

    if not region.is_product_enabled(product.id):
        return problem(400, 'Bad request',
                       f'Product {product.id} is not enabled in the region')

    cluster_data = body.copy()
    cluster_data['created'] = date_now()

    shared = cluster_data.pop('shared', False)
    if shared:
        if not _user_can_create_sharedcluster(user):
            return problem(
                404, 'Forbidden',
                "You don't have necessary permissions to create shared clusters."
            )

        cluster_data['lifespan_expiration'] = None
        cluster_data['reservation_expiration'] = None

    if region.lifespan_enabled:
        if 'lifespan_expiration' in cluster_data:
            if not _user_can_set_lifespan(region, user):
                return problem(
                    403, 'Forbidden',
                    'Only admin and region owner can set lifespan expiration '
                    'on clusters in the selected region.'
                )
            if cluster_data['lifespan_expiration'] is not None:
                cluster_data['lifespan_expiration'] = date_parse(
                    cluster_data['lifespan_expiration']
                )
            else:
                cluster_data['lifespan_expiration'] = None
        else:
            cluster_data['lifespan_expiration'] = (
                cluster_data['created'] + region.lifespan_delta
            )
    else:
        cluster_data['lifespan_expiration'] = None

    if cluster_data.get('reservation_expiration') is not None:
        reservation_expiration = date_parse(cluster_data['reservation_expiration'])
        cluster_data['reservation_expiration'] = reservation_expiration
    else:
        cluster_data['reservation_expiration'] = None

    if region.reservation_expiration_max:
        if cluster_data['reservation_expiration'] is None:
            if not _user_can_disable_expiration(region, user):
                return problem(
                    403, 'Forbidden',
                    'Only admin and region owner can set create clusters without '
                    'expiration in the selected region.'
                )
        else:
            reservation_expiration_max = (
                cluster_data['created'] + region.reservation_expiration_max_delta
            )
            if reservation_expiration > reservation_expiration_max:
                return problem(
                    403, 'Forbidden', 'Exceeded maximal reservation time.',
                    ext={'reservation_expiration_max': reservation_expiration_max},
                )

    cluster_data['product_params'] = (
        product.parameters_defaults | body['product_params']
    )

    if 'project_id' in cluster_data:
        project_id = cluster_data['project_id']
        project = openstack_model.Project.query.get(project_id)
        if not project:
            return problem(404, 'Not Found', f'Project {project_id} does not exist')
        if project.cloud_id != region.openstack.id:
            return problem(
                400, 'Bad Request',
                f'Project {project_id} does not belong to the selected '
                f'OpenStack cloud {region.openstack.id}',
            )
        if project.owner_id != user and not _user_is_cluster_admin(user):
            return problem(
                403, 'Forbidden',
                "You don't have permission to create clusters in the selected project.",
            )

    else:
        user_row = auth_model.User.query.get(user)
        project_name = f'ql_{user_row.name}'

        project_query = openstack_model.Project.query.filter(
            db.and_(
                openstack_model.Project.cloud_id == region.openstack.id,
                openstack_model.Project.name == project_name,
            )
        )
        if project_query.count() > 0:
            project = project_query.first()
        else:
            project = openstack_model.Project(
                cloud_id=region.openstack.id,
                name=project_name,
                description='Project created by QuickCluster playbooks',
                owner_id=user,
                group_id=_get_sharedcluster_group_id() if shared else None
            )
            db.session.add(project)
            db.session.flush()

            logger.info(
                f'Created default QuickCluster project ID={project.id} in '
                f'OpenStack ID={region.openstack.id} for user ID={user}',
                extra={'user_id': user, 'project_id': project.id},
            )

        cluster_data['project_id'] = project.id

    try:
        cluster = model.Cluster.from_dict(cluster_data)
        db.session.add(cluster)
        db.session.flush()
    except ValueError as e:
        return problem(400, 'Bad Request', str(e))

    # We don't validate shared cluster params as they can exceed allowed values
    if not shared:
        try:
            product.validate_cluster_params(cluster.product_params)
        except Exception as e:
            db.session.rollback()
            return problem(400, 'Bad Request', 'Invalid product parameters.',
                           ext={'invalid_product_params': e.args[0]})

    if region.user_quota is not None and product.flavors is not None and not shared:
        try:
            cluster_usage = lab_utils.calculate_cluster_usage(cluster)

            user_quota = region.user_quota.to_dict()
            user_quota_usage = region.get_user_quota_usage(user)

            exceeded_resources = []
            for k in user_quota:
                if (user_quota[k] is not None  # Quota fields are nullable
                        and (user_quota_usage[k] + cluster_usage[k]) > user_quota[k]):
                    exceeded_resources.append(k)

            if exceeded_resources:
                logger.error(
                    f'Refused to create {product.name} cluster for user ID={user}. '
                    f'Quota exceeded: {exceeded_resources!r}.',
                    extra={'user_id': user, 'project_id': project.id},
                )
                return problem(
                    400, 'Bad Request', 'Quota Exceeded. Please resize cluster',
                    ext={'exceeded_resources': exceeded_resources}
                )

        except Exception:
            db.session.rollback()
            logger.exception('Failed to calculate usage of cluster')
            return problem(500, 'Internal Server Error',
                           'Failed to calculate cluster usage.')

    try:
        tower_client = region.tower.create_tower_client()
        tower_template = tower_client.template_get(
            template_name=product.tower_template_name_create,
        )

        logger.info(
            f'Launching Tower template {tower_template["name"]} '
            f'(id={tower_template["id"]}), '
            f'extra_vars={cluster.tower_launch_extra_vars!r}',
            extra={'user_id': user},
        )
        tower_job = tower_client.template_launch(
            tower_template['id'],
            {'extra_vars': cluster.tower_launch_extra_vars},
        )

        cluster_event = model.ClusterTowerJobEvent(
            cluster_id=cluster.id,
            user_id=user,
            date=date_now(),
            tower_id=region.tower_id,
            tower_job_id=tower_job['id'],
            status=model.ClusterStatus.QUEUED,
        )
        db.session.add(cluster_event)

        cluster.status = model.ClusterStatus.QUEUED

    except Exception as e:
        db.session.rollback()
        logger.exception(f'Failed to trigger cluster creation in Tower, {e!s}')
        return problem(500, 'Internal Server Error',
                       'Failed to trigger cluster creation.')

    db.session.commit()

    logger.info(
        f'Cluster {cluster.name} (id {cluster.id}) created by user {user}',
        extra={'user_id': user, 'cluster_id': cluster.id},
    )

    return cluster.to_dict() | {'_href': _cluster_href(cluster)}


def get_cluster(cluster_id, user):
    cluster = model.Cluster.query.get(cluster_id)
    if not cluster:
        return problem(404, 'Not Found', f'Cluster {cluster_id} does not exist')

    if not _user_can_access_cluster(cluster, user) and not cluster.shared:
        return problem(403, 'Forbidden', "You don't have access to this cluster.")

    return cluster.to_dict() | {'_href': _cluster_href(cluster)}


def update_cluster(cluster_id, body, user):
    return update_cluster_extra(cluster_id, {'cluster_data': body}, user)


def update_cluster_extra(cluster_id, body, user):
    cluster = model.Cluster.query.get(cluster_id)
    if not cluster:
        return problem(404, 'Not Found', f'Cluster {cluster_id} does not exist')

    if not _user_can_access_cluster(cluster, user):
        return problem(403, 'Forbidden', "You don't have access to this cluster.")

    if cluster.status.is_deleted:
        return problem(400, 'Bad Request',
                       f"Can't update, cluster {cluster_id} is in deleted state")

    cluster_data = body['cluster_data'].copy()
    tower_job_id = body.get('tower_job_id')

    for key in ['name', 'region_id', 'product_id', 'product_params']:
        if key in cluster_data:
            return problem(400, 'Bad Request',
                           f'Cluster {key} field cannot be changed.')

    if 'lifespan_expiration' in cluster_data:
        if cluster.region.lifespan_enabled:
            if _user_can_set_lifespan(cluster.region, user):
                if cluster_data['lifespan_expiration'] is not None:
                    cluster_data['lifespan_expiration'] = date_parse(
                        cluster_data['lifespan_expiration']
                    )
            else:
                return problem(
                    403, 'Forbidden',
                    'Only admin and region owner can set lifespan expiration '
                    'on clusters in the selected region.'
                )
        else:
            del cluster_data['lifespan_expiration']

        cluster_event = model.ClusterLifespanChangeEvent(
            cluster_id=cluster.id,
            user_id=user,
            date=date_now(),
            old_value=cluster.lifespan_expiration,
            new_value=cluster_data['lifespan_expiration']
        )
        db.session.add(cluster_event)

        logger.info(
            f'User {user} changed lifespan expiration of cluster ID={cluster.id}'
            f'from {cluster_event.old_value} to {cluster_event.new_value}',
            extra={'user_id': user, 'cluster_id': cluster.id},
        )

    if 'reservation_expiration' in cluster_data:
        if cluster_data['reservation_expiration'] is None:
            if not _user_can_disable_expiration(cluster.region, user):
                return problem(
                    403, 'Forbidden',
                    'Only admin and region owner can set create clusters without '
                    'expiration in the selected region.'
                )
        else:
            reservation_expiration = date_parse(cluster_data['reservation_expiration'])
            cluster_data['reservation_expiration'] = reservation_expiration
            if cluster.region.reservation_expiration_max:
                reservation_expiration_max = (
                    (cluster.reservation_expiration or date_now())
                    + cluster.region.reservation_expiration_max_delta
                )
                if cluster.lifespan_expiration:
                    reservation_expiration_max = min(reservation_expiration_max,
                                                     cluster.lifespan_expiration)
                if reservation_expiration > reservation_expiration_max:
                    return problem(
                        403, 'Forbidden', 'Exceeded maximal reservation time.',
                        ext={'reservation_expiration_max': reservation_expiration_max},
                    )

        cluster_event = model.ClusterReservationChangeEvent(
            cluster_id=cluster.id,
            user_id=user,
            date=date_now(),
            old_value=cluster.reservation_expiration,
            new_value=cluster_data['reservation_expiration']
        )
        db.session.add(cluster_event)

        logger.info(
            f'User {user} changed reservation expiration of cluster ID={cluster.id}'
            f'from {cluster_event.old_value} to {cluster_event.new_value}',
            extra={'user_id': user, 'cluster_id': cluster.id},
        )

    if 'status' in cluster_data:
        if not _user_is_cluster_admin(user):
            return problem(
                403, 'Forbidden',
                "You don't have permissions to change the cluster status."
            )

        cluster_data['status'] = model.ClusterStatus(cluster_data['status'])

        cluster_event = model.ClusterTowerJobEvent(
            cluster_id=cluster.id,
            user_id=user,
            date=date_now(),
            status=cluster_data['status'],
            tower_id=cluster.region.tower_id if tower_job_id else None,
            tower_job_id=tower_job_id,
        )
        db.session.add(cluster_event)

        logger.info(
            f'Tower job ID={tower_job_id} changed status of cluster '
            f'ID={cluster.id} {cluster_data["status"]}',
            extra={'cluster_id': cluster.id, 'tower_job_id': tower_job_id},
        )

    cluster.update_from_dict(cluster_data)

    db.session.commit()

    messaging = di.get(Messaging)
    messaging.send(
        'lab.cluster.update',
        f'Cluster "{cluster.name}" (ID={cluster.id}) has been updated.',
        extra={
            'cluster_id': cluster.id,
            'cluster_name': cluster.name,
            'update_data': body['cluster_data'],
            'tower_job_id': body.get('tower_job_id'),
        },
    )

    logger.info(
        f'Cluster {cluster.name} (id {cluster.id}) updated by user {user}',
        extra={'user_id': user, 'cluster_id': cluster.id},
    )

    return cluster.to_dict() | {'_href': _cluster_href(cluster)}


def delete_cluster(cluster_id, user):
    cluster = model.Cluster.query.get(cluster_id)
    if not cluster:
        return problem(404, 'Not Found', f'Cluster {cluster_id} does not exist')

    if not _user_can_access_cluster(cluster, user):
        return problem(403, 'Forbidden', "You don't have access to this cluster.")

    if cluster.status.is_deleting:
        return problem(400, 'Bad Request',
                       f'Cluster {cluster_id} is already in deleting state')

    if cluster.status.is_deleted:
        return problem(400, 'Bad Request',
                       f'Cluster {cluster_id} was already deleted')

    if cluster.status.is_creating:
        return problem(
            400, 'Bad Request',
            f'Cluster {cluster_id} is in creating state. Before deleting, '
            'the cluster must be in the Active state or in any of failed states.',
        )

    try:
        lab_utils.delete_cluster(cluster, user)
    except Exception:
        return problem(500, 'Internal Server Error',
                       'Failed to trigger cluster deletion.')


def list_cluster_events(cluster_id, user):
    cluster = model.Cluster.query.get(cluster_id)
    if not cluster:
        return problem(404, 'Not Found', f'Cluster {cluster_id} does not exist')

    if not _user_can_access_cluster(cluster, user) and not cluster.shared:
        return problem(403, 'Forbidden', "You don't have access to this cluster.")

    return [
        event.to_dict() | {'_href': _cluster_event_href(event)}
        for event in cluster.events
    ]


def get_cluster_event(event_id, user):
    event = model.ClusterEvent.query.get(event_id)
    if not event:
        return problem(404, 'Not Found', f'Event {event_id} does not exist')

    if not _user_can_access_cluster(event.cluster, user) and not event.cluster.shared:
        return problem(403, 'Forbidden', "You don't have access to related cluster.")

    return event.to_dict() | {'_href': _cluster_event_href(event)}


def get_cluster_event_stdout(event_id, user):
    event = model.ClusterTowerJobEvent.query.get(event_id)
    if not event:
        return problem(404, 'Not Found', f'Event {event_id} does not exist')

    if not _user_can_access_cluster(event.cluster, user) and not event.cluster.shared:
        return problem(403, 'Forbidden', "You don't have access to related cluster.")

    try:
        return Response(event.get_tower_job_output(), 200, content_type='text/plain')
    except TowerError as e:
        logger.exception(f'Failed to get job {event.tower_job_id} stdout, {e}')
        return problem(404, 'Error', 'Failed to get output from Tower')


def list_cluster_hosts(cluster_id, user):
    cluster = model.Cluster.query.get(cluster_id)
    if not cluster:
        return problem(404, 'Not Found', f'Cluster {cluster_id} does not exist')

    if not _user_can_access_cluster(cluster, user) and not cluster.shared:
        return problem(403, 'Forbidden', "You don't have access to this cluster.")

    return [
        host.to_dict() | {'_href': _cluster_host_href(host)}
        for host in cluster.hosts
    ]


@auth_utils.route_require_admin
def create_cluster_hosts(cluster_id, body, user):
    cluster = model.Cluster.query.get(cluster_id)
    if not cluster:
        return problem(404, 'Not Found', f'Cluster {cluster_id} does not exist')

    hosts = []
    for host_data in body:
        host = model.ClusterHost.from_dict({'cluster_id': cluster_id, **host_data})
        hosts.append(host)

        db.session.add(host)
        db.session.flush()

        logger.info(
            f'Adding host ID={host.id} FQDN={host.fqdn} to cluster ID={cluster.id}',
            extra={'user_id': user, 'cluster_id': cluster.id},
        )

    db.session.commit()

    return [
        host.to_dict() | {'_href': _cluster_host_href(host)}
        for host in hosts
    ]


@auth_utils.route_require_admin
def delete_cluster_hosts(cluster_id, user):
    cluster = model.Cluster.query.get(cluster_id)
    if not cluster:
        return problem(404, 'Not Found', f'Cluster {cluster_id} does not exist')

    for host in cluster.hosts:
        logger.info(
            f'Deleting host ID={host.id} FQDN={host.fqdn} from cluster ID={cluster.id}',
            extra={'user_id': user, 'cluster_id': cluster.id},
        )
        db.session.delete(host)
        db.session.flush()

    db.session.commit()


def reboot_hosts(cluster_id, body, user):
    cluster = model.Cluster.query.get(cluster_id)
    if not cluster:
        return problem(404, 'Not Found', f'Cluster {cluster_id} does not exist')

    if not _user_can_access_cluster(cluster, user):
        return problem(403, 'Forbidden', "You don't have access to this cluster.")

    reboot_type = body.get('type', 'soft').upper()

    if body['hosts'] == 'all':
        hosts_to_reboot = {host.fqdn: host for host in cluster.hosts}
    else:
        hosts_to_reboot = {
            host.fqdn: host
            for host in model.ClusterHost.query.filter(
                sqlalchemy.and_(
                    model.ClusterHost.cluster_id == cluster.id,
                    sqlalchemy.or_(
                        model.ClusterHost.id.in_(
                            [i['id'] for i in body['hosts'] if 'id' in i]
                        ),
                        model.ClusterHost.fqdn.in_(
                            [i['fqdn'] for i in body['hosts'] if 'fqdn' in i]
                        ),
                    ),
                )
            ).all()
        }

    rebooted_hosts = []

    try:
        os_client = cluster.project.create_openstack_client()
        for server in os_client.compute.servers():
            if server.hostname in hosts_to_reboot:
                logger.info(
                    f'Rebooting cluster host {server.hostname}, '
                    f'cluster_id={cluster.id}',
                    extra={'user_id': user, 'cluster_id': cluster.id},
                )
                os_client.compute.reboot_server(server, reboot_type)
                rebooted_hosts.append(hosts_to_reboot[server.hostname])
    except Exception as e:
        logger.exception(f'Failed to reboot nodes, {e!s}')
        return problem(500, 'Server Error', 'Failed to reboot nodes')

    return [{'id': host.id, 'fqdn': host.fqdn} for host in rebooted_hosts]


def cluster_authorized_keys(cluster_id):
    cluster = model.Cluster.query.get(cluster_id)
    if not cluster:
        return problem(404, 'Not Found', f'Cluster {cluster_id} does not exist')
    return '\n'.join(cluster.authorized_keys) + '\n'
