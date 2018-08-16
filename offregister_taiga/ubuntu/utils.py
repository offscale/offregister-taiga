from cStringIO import StringIO
from functools import partial
from json import dump, load
from os import path

import offregister_rabbitmq.ubuntu as rabbitmq
from fabric.api import run, cd, put, shell_env
from fabric.contrib.files import upload_template, exists, append
from fabric.operations import sudo
from offregister_app_push.ubuntu import build_node_app
from offregister_fab_utils.apt import apt_depends
from offregister_fab_utils.git import clone_or_update
from offregister_fab_utils.ubuntu.systemd import install_upgrade_service
from offregister_postgres.ubuntu import install0 as install_postgres
from offregister_python.ubuntu import install_venv0
from offutils import generate_temp_password
from pkg_resources import resource_filename

taiga_dir = partial(path.join, path.dirname(resource_filename('offregister_taiga', '__init__.py')), 'data')


def install_python_taiga_deps(cloned_xor_updated, server_name, virtual_env, sample_data=False):
    apt_depends('build-essential', 'binutils-doc', 'autoconf', 'flex', 'bison',
                'libjpeg-dev', 'libfreetype6-dev', 'zlib1g-dev', 'libzmq3-dev',
                'libgdbm-dev', 'libncurses5-dev', 'automake', 'libtool',
                'libffi-dev', 'curl', 'git', 'tmux', 'gettext', 'libxml2-dev',
                'libxslt-dev', 'libssl-dev', 'libffi-dev')

    sudo('mkdir -p {virtual_env}'.format(virtual_env=virtual_env))
    group_user = run('''printf '%s:%s' "$USER" $(id -gn)''', shell_escape=False, quiet=True)
    sudo('chown -R {group_user} {virtual_env}'.format(group_user=group_user, virtual_env=virtual_env))
    install_venv0(python3=True, virtual_env=virtual_env, pip_version='9.0.3')

    with shell_env(VIRTUAL_ENV=virtual_env, PATH="{}/bin:$PATH".format(virtual_env)), cd('taiga-back'):
        # run("sed -i '0,/lxml==3.5.0b1/s//lxml==3.5.0/' requirements.txt")
        run('pip3 --version; python3 --version')
        run('pip3 install ipython')
        run('pip3 install -r requirements.txt')

        if cloned_xor_updated == 'cloned':
            upload_template(taiga_dir('django.settings.py'), 'settings/local.py',
                            context={'SERVER_NAME': server_name})
            run('python3 manage.py migrate --noinput')
            run('python3 manage.py compilemessages')
            run('python3 manage.py collectstatic --noinput')
            run('python3 manage.py loaddata initial_user')
            run('python3 manage.py loaddata initial_project_templates')
            if sample_data:
                run('python3 manage.py sample_data')
            run('python3 manage.py rebuild_timeline --purge')
        else:
            run('python3 manage.py migrate --noinput')
            run('python3 manage.py compilemessages')
            run('python3 manage.py collectstatic --noinput')
    return virtual_env


def _install_frontend(taiga_root=None, **kwargs):
    apt_depends('git')
    remote_taiga_root = taiga_root or run('printf $HOME', quiet=True)

    sudo('mkdir -p {root}/logs'.format(root=remote_taiga_root))
    group_user = run('''printf '%s:%s' "$USER" $(id -gn)''', shell_escape=False, quiet=True)
    sudo('chown -R {group_user} {root}'.format(group_user=group_user, root=remote_taiga_root))

    with cd(remote_taiga_root):
        clone_or_update(team='taigaio', repo='taiga-front')
        # Compile it here if you prefer
        if not exists('taiga-front/dist'):
            clone_or_update(team='taigaio', repo='taiga-front-dist')
            run('ln -s {root}/taiga-front-dist/dist {root}/taiga-front/dist'.format(root=remote_taiga_root))

    sudo('mkdir -p /etc/nginx/sites-enabled')

    upload_template(taiga_dir('taiga.nginx.conf'), '/etc/nginx/sites-enabled/taiga.conf',
                    context={'TAIGA_ROOT': remote_taiga_root,
                             'LISTEN_PORT': kwargs['LISTEN_PORT'],
                             'SERVER_NAME': kwargs['SERVER_NAME']},
                    use_sudo=True)


def _install_backend(server_name, taiga_root=None, database=True, database_uri=None, remote_user=None):
    apt_depends('git')  # 'circus'
    remote_taiga_root = taiga_root or run('printf $HOME', quiet=True)
    virtual_env = '/opt/venvs/taiga'

    if database:
        remote_user = remote_user or 'ubuntu'
        install_postgres(dbs=('taiga', remote_user), users=(remote_user,))
    elif not database_uri:
        raise ValueError('Must create database or provide database_uri')

    with cd(remote_taiga_root):
        install_python_taiga_deps(clone_or_update(team='taigaio', repo='taiga-back'),
                                  server_name=server_name, virtual_env=virtual_env)

    # Circus
    circus_env = '/opt/venvs/circus'
    sudo('mkdir -p {circus_env}'.format(circus_env=circus_env))
    group_user = run('''printf '%s:%s' "$USER" $(id -gn)''', shell_escape=False, quiet=True)
    sudo('chown -R {group_user} {circus_env}'.format(group_user=group_user, circus_env=circus_env))
    install_venv0(python3=False, virtual_env=circus_env)
    with shell_env(VIRTUAL_ENV=circus_env, PATH="{}/bin:$PATH".format(circus_env)):
        run('pip2 install circus')

    conf_dir = '/etc/circus/conf.d'  # '/'.join((remote_taiga_root, 'config'))
    sudo('mkdir -p {conf_dir}'.format(conf_dir=conf_dir))

    py_ver = run('{virtual_env}/bin/python --version'.format(virtual_env=virtual_env)).partition(' ')[2][:3]

    upload_template(taiga_dir('circus.ini'), '{conf_dir}/'.format(conf_dir=conf_dir),
                    context={'HOME': remote_taiga_root, 'USER': remote_user,
                             'VENV': virtual_env, 'PYTHON_VERSION': py_ver},
                    use_sudo=True)
    circusd_context = {'CONF_DIR': conf_dir, 'CIRCUS_VENV': circus_env}
    # upload_template(taiga_dir('circusd.conf'), '/etc/init/', context=circusd_context, use_sudo=True)
    upload_template(taiga_dir('circusd.service'), '/etc/systemd/system/', context=circusd_context, use_sudo=True)


def _install_events(taiga_root):
    rabbitmq.install0()  # install_rabbitmq()
    user = 'taiga'

    if sudo('rabbitmqctl list_users | grep -q {user}'.format(user=user), warn_only=True).succeeded:
        return

    password = rabbitmq.create_user1(rmq_user=user, rmq_vhost=user)

    rmq_uri = 'amqp://{user}:{password}@localhost:5672/{user}'.format(user=user, password=password)

    append('{taiga_root}/taiga-back/settings/local.py'.format(taiga_root=taiga_root),
           'EVENTS_PUSH_BACKEND = "taiga.events.backends.rabbitmq.EventsPushBackend"\n'
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


def _replace_configs(server_name, listen_port, taiga_root, email, public_register_enabled, force_clean=False):
    remote_taiga_root = taiga_root or run('printf $HOME', quiet=True)

    protocol = 'https' if listen_port == 443 else 'http'

    fqdn = '{protocol}://{server_name}'.format(protocol=protocol, server_name=server_name)

    # Frontend
    js_conf_dir = '/'.join(('taiga-front', 'dist', 'js'))
    conf_json_fname = '/'.join((js_conf_dir, 'conf.json'))
    if force_clean:
        run('rm -rfv {}'.format(conf_json_fname))
    if not exists(conf_json_fname):
        run('mkdir -p {conf_dir}'.format(conf_dir=js_conf_dir))

        with open(taiga_dir('conf.json')) as f:
            conf = load(f)

        conf['api'] = '{fqdn}{path}'.format(fqdn=fqdn, path=conf['api'])

        event_config = '{taiga_root}/taiga-events/config.json'.format(taiga_root=taiga_root)
        if exists(event_config):
            apt_depends('jq')
            conf['eventsUrl'] = run('jq -r .url {event_config}'.format(event_config=event_config))
        sio = StringIO()
        dump(conf, sio)
        put(sio, conf_json_fname)

    # Backend
    local_py = '{taiga_root}/taiga-back/settings/local.py'.format(taiga_root=taiga_root)
    if force_clean:
        run('rm -rfv {}'.format(local_py))
    if not exists(local_py):
        upload_template(taiga_dir('local.py'), local_py,
                        context={'FQDN': fqdn, 'PROTOCOL': protocol,
                                 'SERVER_NAME': server_name,
                                 'SECRET_KEY': generate_temp_password(52),
                                 'DEFAULT_FROM_EMAIL': email or 'no-reply@example.com',
                                 'PUBLIC_REGISTER_ENABLED': public_register_enabled
                                 })

    # Everything
    run("find {taiga_root} -type f -exec sed -i 's|http://localhost:8000|{fqdn}|g' ".format(
        taiga_root=remote_taiga_root, fqdn=fqdn
    ) + "{} \;", shell_escape=False)
    run("find {taiga_root} -type f -exec sed -i 's|localhost:8000|{server_name}|g' ".format(
        taiga_root=remote_taiga_root, server_name=server_name
    ) + "{} \;", shell_escape=False)
