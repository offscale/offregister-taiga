from functools import partial
from os import path

from fabric.contrib.files import upload_template
from offregister_fab_utils.ubuntu.systemd import restart_systemd
from pkg_resources import resource_filename

from fabric.api import run, cd, put, shell_env
from fabric.operations import sudo
from offregister_fab_utils.apt import apt_depends
from offregister_python.ubuntu import install_venv0

taiga_dir = partial(path.join, path.dirname(resource_filename('offregister_taiga', '__init__.py')), 'data')


def install_python_taiga_deps(cloned_xor_updated, server_name, virtual_env):
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
            run('python3 manage.py sample_data')
            run('python3 manage.py rebuild_timeline --purge')
        else:
            run('python3 manage.py migrate --noinput')
            run('python3 manage.py compilemessages')
            run('python3 manage.py collectstatic --noinput')
    return virtual_env
