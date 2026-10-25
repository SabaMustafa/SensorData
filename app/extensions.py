from flask_sqlalchemy import SQLAlchemy

# Created here (not in create_app) so models and routes can import it
db = SQLAlchemy()
