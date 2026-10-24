import os

class Config:
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or 'sqlite:///gobd_ledger.db'
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'super-secret-key'
    PAPERLESS_URL = os.environ.get('PAPERLESS_URL', 'http://paperless-ngx:8000')
    PAPERLESS_API_TOKEN = os.environ.get('PAPERLESS_API_TOKEN', '')
    INVOICE_NINJA_URL = os.environ.get('INVOICE_NINJA_URL', 'http://invoiceninja-web:80')

class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    SECRET_KEY = 'test-secret-key'
