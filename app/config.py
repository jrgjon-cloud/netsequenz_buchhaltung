import os

class Config:
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or 'sqlite:///gobd_ledger.db'
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'super-secret-key'
    PAPERLESS_URL = os.environ.get('PAPERLESS_URL', 'http://paperless-ngx:8000')
    PAPERLESS_API_TOKEN = os.environ.get('PAPERLESS_API_TOKEN', '')
    PAPERLESS_PUBLIC_URL = os.environ.get('PAPERLESS_PUBLIC_URL', 'https://tax-dashboard-server:8000')
    INVOICE_NINJA_URL = os.environ.get('INVOICE_NINJA_URL', 'http://invoiceninja-web:80')
    INVOICE_NINJA_PUBLIC_URL = os.environ.get('INVOICE_NINJA_PUBLIC_URL', 'https://tax-dashboard-server:8080')
    INVOICE_NINJA_API_TOKEN = os.environ.get('INVOICE_NINJA_API_TOKEN', '')
    GOCARDLESS_ACCESS_TOKEN = os.environ.get('GOCARDLESS_ACCESS_TOKEN', '')
    GOCARDLESS_ENV = os.environ.get('GOCARDLESS_ENV', 'live')
    SUMUP_API_KEY = os.environ.get('SUMUP_API_KEY', '')

class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    SECRET_KEY = 'test-secret-key'
