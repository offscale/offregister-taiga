from cStringIO import StringIO
from functools import partial
from json import dump, load, dumps
from os import path
from urlparse import urlparse
from pkg_resources import resource_filename

from fabric.api import run, cd, put, shell_env
from fabric.context_managers import settings
from fabric.contrib.files import upload_template, exists, append
from fabric.operations import sudo

import offregister_rabbitmq.ubuntu as rabbitmq
from offregister_app_push.ubuntu import build_node_app
from offregister_fab_utils import macos
from offregister_fab_utils.apt import apt_depends
from offregister_fab_utils.fs import cmd_avail
from offregister_fab_utils.git import clone_or_update
from offregister_fab_utils.ubuntu import systemd
from offregister_postgres import ubuntu as postgres
from offregister_postgres.utils import get_postgres_params
from offregister_python.ubuntu import install_venv0
from offutils import generate_random_alphanum

taiga_dir = partial(path.join, path.dirname(resource_filename('offregister_taiga', '__init__.py')), 'data')


def install_python_taiga_deps(virtual_env):
    uname = run('uname -v')

    is_ubuntu = 'Ubuntu' in uname

    if is_ubuntu:
        apt_depends('build-essential', 'binutils-doc', 'autoconf', 'flex', 'bison',
                    'libjpeg-dev', 'libfreetype6-dev', 'zlib1g-dev', 'libzmq3-dev',
                    'libgdbm-dev', 'libncurses5-dev', 'automake', 'libtool',
                    'libffi-dev', 'curl', 'git', 'tmux', 'gettext', 'libxml2-dev',
                    'libxslt1-dev', 'libssl-dev', 'libffi-dev', 'libffi6')
    elif uname.startswith('Darwin'):
        run('brew install libxml2 libxslt')
    else:
        raise NotImplementedError(uname)

    sudo('mkdir -p {virtual_env}'.format(virtual_env=virtual_env))
    group_user = run('''printf '%s:%s' "$USER" $(id -gn)''', shell_escape=False, quiet=True)
    sudo('chown -R {group_user} {virtual_env}'.format(group_user=group_user, virtual_env=virtual_env))

    pip_version = '19.1'

    if is_ubuntu:
        install_venv0(python3=True, virtual_env=virtual_env, pip_version=pip_version)
    else:
        run('python3 -m venv "{virtual_env}" '.format(virtual_env=virtual_env),
            shell_escape=False)

    with shell_env(VIRTUAL_ENV=virtual_env, PATH='{}/bin:$PATH'.format(virtual_env)), cd('taiga-back'):
        # run("sed -i '0,/lxml==3.5.0b1/s//lxml==3.5.0/' requirements.txt")
        run('pip3 install -U pip')
        run('pip3 --version; python3 --version')

        if not is_ubuntu:
            run('STATIC_DEPS=true pip3 install lxml')

        run('pip3 install -U setuptools')
        run('pip3 install --no-cache-dir cffi')
        run('pip3 install --no-cache-dir cairocffi')
        run('pip3 install -r requirements.txt')
    return virtual_env


def _migrate(virtual_env, taiga_root, skip_migrate, sample_data, remote_user, database_uri):
    if skip_migrate:
        return virtual_env

    with shell_env(VIRTUAL_ENV=virtual_env, PATH='{}/bin:$PATH'.format(virtual_env)
                   ), cd('{taiga_root}/taiga-back'.format(taiga_root=taiga_root)):
        DATABASES = {
            'default': {
                'ENGINE': 'django.db.backends.postgresql',
                'NAME': 'mydatabase',
                'USER': 'mydatabaseuser',
                'PASSWORD': 'mypassword',
                'HOST': '127.0.0.1',
                'PORT': '5432',
            }
        }

        if database_uri:
            parsed_connection_str = urlparse(database_uri)

            # TODO: Put this all back in offregister-postgres
            params = get_postgres_params(parsed_connection_str)

            postgres = partial(sudo, user='postgres', shell_escape=False)

            if postgres(
                '''[ -f ~/.bash_profile ] && source ~/.bash_profile ; psql -t -c '\l' "{database_uri}" | grep -qF taiga'''.format(
                    database_uri=database_uri),
                warn_only=True).failed:
                with settings(prompts={'Password: ': parsed_connection_str.password}):
                    postgres('[ -f ~/.bash_profile ] && source ~/.bash_profile ; createdb {params} {dbname}'.format(
                        params=params, dbname='taiga'))
            # ENDTODO

            DATABASES['default']['NAME'] = 'taiga'
            DATABASES['default']['USER'] = parsed_connection_str.username
            DATABASES['default']['PASSWORD'] = parsed_connection_str.password
            DATABASES['default']['HOST'] = parsed_connection_str.hostname
            DATABASES['default']['PORT'] = parsed_connection_str.port or DATABASES['default']['PORT']

            # TODO: Use my Django settings.py parser/emitter
            append('settings/local.py', 'DATABASES = {}'.format(dumps(DATABASES, sort_keys=True)))

        run('python3 manage.py migrate --noinput')
        run('python3 manage.py compilemessages')
        run('python3 manage.py collectstatic --noinput')

        run('python3 manage.py loaddata initial_user')
        run('python3 manage.py loaddata initial_project_templates')

        if sample_data:
            run('python3 manage.py sample_data')
        run('python3 manage.py rebuild_timeline --purge')

    return virtual_env


def _install_frontend(taiga_root=None, **kwargs):
    sudo('mkdir -p {root}/logs'.format(root=taiga_root))
    group_user = run('''printf '%s:%s' "$USER" $(id -gn)''', shell_escape=False, quiet=True)
    sudo('chown -R {group_user} {root}'.format(group_user=group_user, root=taiga_root))

    with cd(taiga_root):
        clone_or_update(team='taigaio', repo='taiga-front')
        # Compile it here if you prefer
        if not exists('taiga-front/dist'):
            clone_or_update(team='taigaio', repo='taiga-front-dist')
            run('ln -s {root}/taiga-front-dist/dist {root}/taiga-front/dist'.format(root=taiga_root))

    if not kwargs.get('skip_nginx'):
        sudo('mkdir -p /etc/nginx/sites-enabled')

        upload_template(taiga_dir('taiga.nginx.conf'), '/etc/nginx/sites-enabled/taiga.conf',
                        context={'TAIGA_ROOT': taiga_root,
                                 'LISTEN_PORT': kwargs['LISTEN_PORT'],
                                 'SERVER_NAME': kwargs['SERVER_NAME']},
                        use_sudo=True)


def _install_backend(taiga_root, remote_user, circus_virtual_env,
                     virtual_env, database=True, database_uri=''):
    # apt_depends('circus')
    uname = run('uname -v')
    is_ubuntu = 'Ubuntu' in uname
    home = run('echo $HOME')

    # /print 'postgres.setup_users'
    # postgres.setup_users(connection_str=database_uri, dbs=('taiga', remote_user), users=(remote_user,))
    # /print '/postgres.setup_users'

    if database:
        if is_ubuntu:
            postgres.install0()
        postgres.setup_users(dbs=('taiga', remote_user), users=(remote_user,))
    elif not database_uri:
        raise ValueError('Must create database or provide database_uri')

    with cd(taiga_root):
        clone_or_update(team='SamuelMarks', repo='taiga-back')
        install_python_taiga_deps(virtual_env)

    # Circus
    sudo('mkdir -p {circus_virtual_env}'.format(circus_virtual_env=circus_virtual_env))
    group_user = run('''printf '%s:%s' "$USER" $(id -gn)''', shell_escape=False, quiet=True)
    sudo('chown -R {group_user} {circus_virtual_env}'.format(group_user=group_user,
                                                             circus_virtual_env=circus_virtual_env))

    print 'before install_venv0', circus_virtual_env

    if is_ubuntu:
        install_venv0(python3=False, virtual_env=circus_virtual_env)
    else:
        if not cmd_avail('virtualenv'):
            run('pip install virtualenv')

        run('virtualenv "{virtual_env}"'.format(virtual_env=circus_virtual_env))
    with shell_env(VIRTUAL_ENV=circus_virtual_env, PATH="{}/bin:$PATH".format(circus_virtual_env)):
        run('pip2 install circus')

    print 'after install_venv0', circus_virtual_env

    conf_dir = '/etc/circus/conf.d'  # '/'.join((taiga_root, 'config'))
    sudo('mkdir -p {conf_dir}'.format(conf_dir=conf_dir))

    py_ver = run('{virtual_env}/bin/python --version'.format(virtual_env=circus_virtual_env)).partition(' ')[2][:3]

    upload_template(taiga_dir('circus.ini'), '{conf_dir}/'.format(conf_dir=conf_dir),
                    context={'HOME': taiga_root, 'USER': remote_user,
                             'VENV': circus_virtual_env, 'PYTHON_VERSION': py_ver},
                    use_sudo=True)
    circusd_context = {'CONF_DIR': conf_dir, 'CIRCUS_VENV': circus_virtual_env}
    if uname.startswith('Darwin'):
        upload_template(taiga_dir('circusd.launchd.xml'),
                        '{home}/Library/LaunchAgents/io.readthedocs.circus.plist'.format(home=home),
                        context=circusd_context)
    elif exists('/etc/systemd/system'):
        upload_template(taiga_dir('circusd.service'), '/etc/systemd/system/', context=circusd_context, use_sudo=True)
    else:
        upload_template(taiga_dir('circusd.conf'), '/etc/init/', context=circusd_context, use_sudo=True)

    return circus_virtual_env


def _install_events(taiga_root):
    uname = run('uname -v')
    is_ubuntu = 'Ubuntu' in uname

    if not cmd_avail('rabbitmqctl'):
        if is_ubuntu:
            rabbitmq.install0()
        elif 'Darwin' in uname:
            run('brew install rabbitmq')
            if not cmd_avail('rabbitmqctl'):
                append('$HOME/.bash_profile', 'export PATH=/usr/local/sbin:$PATH')
                run('logout')
            run('brew services start rabbitmq')
        else:
            raise NotImplementedError(uname)
    user = 'taiga'

    if sudo('rabbitmqctl list_users | grep -q {user}'.format(user=user), warn_only=True).succeeded:
        return

    password = rabbitmq.create_user1(rmq_user=user, rmq_vhost=user)

    rmq_uri = 'amqp://{user}:{password}@localhost:5672/{user}'.format(user=user, password=password)

    event_root = '{taiga_root}/taiga-events'.format(taiga_root=taiga_root)
    clone_or_update(team='taigaio', repo='taiga-events', branch='master', to_dir=event_root)
    with cd(event_root):
        build_node_app(kwargs=dict(npm_global_packages=('coffeescript',), node_version='lts'),
                       run_cmd=run)
    upload_template(taiga_dir('config.json'), event_root, context={'RMQ_URI': rmq_uri})

    user = run('echo $USER', quiet=True)
    if cmd_avail('systemctl'):
        return systemd.install_upgrade_service(
            service_name='taiga_events',
            context={
                'User': user, 'Group': run('id -gn') or user,
                'Environments': '', 'WorkingDirectory': event_root,
                'ExecStart': "/bin/bash -c 'PATH=/home/{user}/n/bin:$PATH /home/{user}/n/bin/coffee index.coffee'".format(
                    user=user
                )
            }
        )
    elif uname.startswith('Darwin'):
        return macos.install_upgrade_service(
            'io.taiga.events',
            context={'PROGRAM': "/bin/bash -c 'PATH=/home/{user}/n/bin:$PATH /home/{user}/n/bin/coffee index.coffee'"}
        )
    raise NotImplementedError(uname)


def _replace_configs(server_name, listen_port, taiga_root, email, public_register_enabled, force_clean=False):
    protocol = 'https' if listen_port == 443 else 'http'

    fqdn = '{protocol}://{server_name}'.format(protocol=protocol, server_name=server_name)

    # Frontend
    js_conf_dir = '/'.join((taiga_root, 'taiga-front', 'dist', 'js'))
    conf_json_fname = '/'.join((js_conf_dir, 'conf.json'))
    if force_clean:
        run('rm -rfv {}'.format(conf_json_fname))
    print 'looking for: {}'.format(conf_json_fname)
    if not exists(conf_json_fname):
        run('mkdir -p {conf_dir}'.format(conf_dir=js_conf_dir))

        with open(taiga_dir('conf.json')) as f:
            conf = load(f)

        conf['api'] = '{fqdn}{path}'.format(fqdn=fqdn, path=conf['api'])

        event_config = '{taiga_root}/taiga-events/config.json'.format(taiga_root=taiga_root)
        if exists(event_config):
            if not cmd_avail('jq'):
                apt_depends('jq')
            conf['eventsUrl'] = run('jq -r .url {event_config}'.format(event_config=event_config))
        sio = StringIO()
        dump(conf, sio, indent=4, sort_keys=True)
        put(sio, conf_json_fname)

    # Backend
    local_py = '{taiga_root}/taiga-back/settings/local.py'.format(taiga_root=taiga_root)
    if force_clean:
        run('rm -rfv {}'.format(local_py))

    stat = run("stat -c'%s' {}".format(local_py), warn_only=True)

    if stat.failed or int(stat) == 0:
        upload_template(taiga_dir('local.py'), local_py,
                        context={'FQDN': fqdn, 'PROTOCOL': protocol,
                                 'SERVER_NAME': server_name,
                                 'SECRET_KEY': generate_random_alphanum(52),
                                 'DEFAULT_FROM_EMAIL': email or 'no-reply@example.com',
                                 'PUBLIC_REGISTER_ENABLED': public_register_enabled
                                 })

    stat = run("stat -c'%s' {}".format(local_py), warn_only=True)
    if stat.failed or int(stat) == 0:
        raise IOError('{} doesn\'t exist; did you install?'.format(local_py))

    cmd = "sed -i -e 's|http://localhost:8000|{fqdn}|g' " \
          "-e 's|localhost:8000|{server_name}|g' " \
          "-e 's|http|{protocol}|g' " \
          "-e 's|httphttp|http|g' " \
          "-e 's|https\\+|https|g' ".format(fqdn=fqdn, server_name=server_name, protocol=protocol)

    # Back
    run('for f in {taiga_root}/taiga-back/settings/*; do {cmd} "$f"; done'.format(cmd=cmd, taiga_root=taiga_root),
        shell_escape=False, shell=False)

    # Front
    run('{cmd} {taiga_root}/taiga-front/app-loader/app-loader.coffee'.format(cmd=cmd, taiga_root=taiga_root))

    # Front (dist)
    run('for f in {taiga_root}/taiga-front-dist/dist/*.json; do {cmd} "$f"; done'.format(
        cmd=cmd, taiga_root=taiga_root), shell_escape=False, shell=False)

    # Everything
    '''
    run('find {taiga_root} -type f -name .git -prune -o {extra} -exec {cmd} {end}'.format(
        taiga_root=taiga_root,
        extra='-exec grep -Iq . {} \\; -and -print',
        cmd=cmd,
        end='{} \;'
    ), shell_escape=False)
    '''
