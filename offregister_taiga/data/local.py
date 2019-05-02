from .common import *

MEDIA_URL = "%(FQDN)s/media/"
STATIC_URL = "%(FQDN)s/static/"

# This should change if you want generate urls in emails
# for external dns.
SITES["front"]["scheme"] = "%(PROTOCOL)s"
SITES["front"]["domain"] = "%(SERVER_NAME)s"

SECRET_KEY = '%(SECRET_KEY)s'

DEBUG = True
PUBLIC_REGISTER_ENABLED = %(PUBLIC_REGISTER_ENABLED)r

DEFAULT_FROM_EMAIL = "%(DEFAULT_FROM_EMAIL)s"
SERVER_EMAIL = DEFAULT_FROM_EMAIL

EVENTS_PUSH_BACKEND = "%(EVENTS_PUSH_BACKEND)s"
EVENTS_PUSH_BACKEND_OPTIONS = %(EVENTS_PUSH_BACKEND_OPTIONS)r

DATABASES = %(DATABASES)r

#EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
#EMAIL_USE_TLS = False
#EMAIL_HOST = "localhost"
#EMAIL_HOST_USER = ""
#EMAIL_HOST_PASSWORD = ""
#EMAIL_PORT = 25
