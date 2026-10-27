from flask import Blueprint, abort, current_app, redirect, render_template, url_for

from ..extensions import db
from ..models import Hive

dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.route("/")
def home():
    # Landing page: who we are, the research and the publications
    hive = Hive.query.order_by(Hive.id).first()
    return render_template("home.html", hive=hive, title=current_app.config["DASHBOARD_TITLE"])


@dashboard_bp.route("/dashboard")
def dashboard():
    # One hive for now: go straight to its dashboard
    hive = Hive.query.order_by(Hive.id).first()
    return redirect(url_for("dashboard.hive_dashboard", hive_id=hive.id))


@dashboard_bp.route("/hives/<int:hive_id>")
def hive_dashboard(hive_id):
    hive = db.session.get(Hive, hive_id) or abort(404)
    has_camera = hive.id in current_app.extensions.get("cameras", {})

    return render_template(
        "dashboard.html",
        hive=hive,
        has_camera=has_camera,
        title=current_app.config["DASHBOARD_TITLE"],
        subtitle=current_app.config["DASHBOARD_SUBTITLE"],
        model_info=current_app.config["MODEL_INFO"],
    )


@dashboard_bp.route("/mobile")
def mobile():
    return render_template("mobile.html", title=current_app.config["DASHBOARD_TITLE"])
