from functools import partial
from os import path

import offregister_rabbitmq.ubuntu as rabbitmq
from fabric.api import run, cd, put
from fabric.contrib.files import exists, upload_template, append
from fabric.operations import sudo
from offregister_app_push.ubuntu import build_node_app
from offregister_fab_utils.apt import apt_depends
from offregister_fab_utils.git import clone_or_update
from offregister_fab_utils.ubuntu.systemd import restart_systemd, install_upgrade_service
from offregister_postgres.ubuntu import install0 as install_postgres
# import install0 as install_rabbitmq, create_user1 as create_rabbitmq_user1
from pkg_resources import resource_filename

from offregister_taiga.ubuntu.utils import install_python_taiga_deps

taiga_dir = partial(path.join, path.dirname(resource_filename('offregister_taiga', '__init__.py')), 'data')


def _install_events(taiga_root):
    rabbitmq.install0()  # install_rabbitmq()
    user = 'taiga'

    if sudo('rabbitmqctl list_users | grep -q {user}'.format(user=user), warn_only=True).failed:
        password = rabbitmq.create_user1(  # create_rabbitmq_user1,
            rmq_user=user, rmq_vhost=user)

        rmq_uri = 'amqp://{user}:{password}@localhost:5672/{user}'.format(user=user, password=password)

        append('{taiga_root}/taiga-back/settings/local.py'.format(taiga_root=taiga_root),
               'EVENTS_PUSH_BACKEND = "taiga.events.backends.rabbitmq.EventsPushBackend"'
               'EVENTS_PUSH_BACKEND_OPTIONS = {"url": "' + rmq_uri + '"}')
    event_root = '{taiga_root}/taiga-events'.format(taiga_root=taiga_root)
    clone_or_update(team='taigaio', repo='taiga-events', branch='master', to_dir=event_root)
    with cd(event_root):
        build_node_app(kwargs=dict(npm_global_packages=('coffeescript',), node_version='lts'),
                       run_cmd=run)
    upload_template(taiga_dir('config.json'), event_root, context={'RMQ_URI': rmq_uri})
    user = run('echo $USER', quiet=True)
    return install_upgrade_service(service_name='taiga_events',
                                   context={
                                       'User': user, 'Group': run('id -gn') or user,
                                       'Environments': '', 'WorkingDirectory': event_root,
                                       'ExecStart': "/bin/bash -c 'PATH=/home/{user}/n/bin:$PATH /home/{user}/n/bin/coffee index.coffee'".format(
                                           user=user)})


def install0(*args, **kwargs):
    _install_frontend(taiga_root=kwargs.get('TAIGA_ROOT'), **kwargs)
    _install_backend(taiga_root=kwargs.get('TAIGA_ROOT'), remote_user=kwargs.get('remote_user'),
                     server_name=kwargs['SERVER_NAME'])
    _install_events(taiga_root=kwargs.get('TAIGA_ROOT'))
    return 'installed taiga'


def serve1(*args, **kwargs):
    restart_systemd('nginx')
    restart_systemd('circusd')
    return 'served taiga'


def _install_frontend(taiga_root=None, **kwargs):
    apt_depends('git')
    remote_taiga_root = taiga_root or run('printf $HOME', quiet=True)

    sudo('mkdir -p {root}/logs'.format(root=remote_taiga_root))
    group_user = run('''printf '%s:%s' "$USER" $(id -gn)''', shell_escape=False, quiet=True)
    sudo('chown -R {group_user} {root}'.format(group_user=group_user, root=remote_taiga_root))

    with cd(remote_taiga_root):
        repo = 'taiga-front'
        clone_or_update(team='taigaio', repo=repo)
        # Compile it here if you prefer
        if not exists('taiga-front/dist'):
            clone_or_update(team='taigaio', repo='taiga-front-dist')
            run('ln -s {root}/taiga-front-dist/dist {root}/taiga-front/dist'.format(root=remote_taiga_root))
        js_conf_dir = '/'.join((repo, 'dist', 'js'))
        if not exists('/'.join((js_conf_dir, 'conf.json'))):
            run('mkdir -p {conf_dir}'.format(conf_dir=js_conf_dir))
            put(taiga_dir('conf.json'), js_conf_dir)

    upload_template(taiga_dir('taiga.nginx.conf'), '/etc/nginx/sites-enabled/taiga.conf',
                    context={'TAIGA_ROOT': remote_taiga_root,
                             'LISTEN_PORT': kwargs['LISTEN_PORT'],
                             'SERVER_NAME': kwargs['SERVER_NAME']},
                    use_sudo=True)


def _install_backend(server_name, taiga_root=None, database=True, database_uri=None, remote_user=None):
    apt_depends('git', 'circus')
    remote_taiga_root = taiga_root or run('printf $HOME', quiet=True)
    if database:
        remote_user = remote_user or 'ubuntu'
        install_postgres(dbs=('taiga', remote_user), users=(remote_user,))
    elif not database_uri:
        raise ValueError('Must create database or provide database_uri')

    with cd(remote_taiga_root):
        virtual_env = install_python_taiga_deps(clone_or_update(team='taigaio', repo='taiga-back'),
                                                server_name=server_name)

    conf_dir = '/'.join((remote_taiga_root, 'config'))
    if not exists('/'.join((conf_dir, 'conf.json'))):
        run('mkdir -p {conf_dir}'.format(conf_dir=conf_dir))
        upload_template(taiga_dir('circus.ini'), conf_dir,
                        context={'HOME': remote_taiga_root, 'USER': remote_user, 'VENV': virtual_env})

    upload_template(taiga_dir('circusd.conf'), '/etc/init/', context={'CONF_DIR': conf_dir}, use_sudo=True)
