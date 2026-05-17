from flask import current_app, redirect, render_template, url_for
from flask_wtf import FlaskForm
from wtforms import StringField, SubmitField, BooleanField, SelectField, IntegerField, RadioField
from wtforms.validators import DataRequired, Optional
from ..models.YaStation import YaStation
from app.database import db

# Определение класса формы
class StationForm(FlaskForm):
    title = StringField('Title', validators=[DataRequired()])
    platform = StringField('Platform')
    iot_id = StringField("IOT id")
    ip = StringField("IP")
    tts = RadioField(
        'TTS',
        choices=[(0, 'No'), (1, 'Local (Glagol / LAN)'), (2, 'Cloud')],
        default=0,
    )
    min_level = StringField("Min level SAY")
    device_token = StringField("Token")
    glagol_linked_object = StringField(
        "Объект osysHome (имя)",
        validators=[Optional()],
    )
    submit = SubmitField('Submit')


def _glagol_station_api_url(station_id) -> str:
    """URL JSON API плеера (тот же префикс, что у ``/admin/<плагин>``)."""
    if station_id is None or station_id == "":
        return ""
    try:
        sid = int(station_id)
    except (TypeError, ValueError):
        return ""
    for ep in current_app.view_functions:
        if ep.endswith("yandexdevices_station_glagol"):
            return url_for(ep, station_id=sid)
    return ""


def editStation(request, plugin=None):
    station_id = request.args.get("station", None)
    station = YaStation.get_by_id(station_id)
    form = StationForm(obj=station)  # Передаем объект в форму для редактирования
    
    if form.validate_on_submit():
        if station_id:
            form.populate_obj(station)  # Обновляем значения объекта данными из формы
            db.session.commit()  # Сохраняем изменения в базе данных
            return redirect("YandexDevices")

    glagol_ws_status = {
        "phase": "off",
        "text": "Реестр Glagol не инициализирован",
        "detail": None,
        "frames_rx": 0,
        "frames_tx": 0,
        "last_rx_at": None,
        "connected_since": None,
    }
    if plugin is not None and station_id is not None:
        reg = getattr(plugin, "_glagol_registry", None)
        if reg is not None:
            try:
                glagol_ws_status = reg.get_station_status(int(station_id))
            except (TypeError, ValueError):
                pass

    glagol_ws_status_url = ""
    for ep in current_app.view_functions:
        if str(ep).endswith("yandexdevices_glagol_ws_status"):
            try:
                glagol_ws_status_url = url_for(ep)
            except Exception:
                glagol_ws_status_url = ""
            break

    glagol_token_exp_hint = ""
    glagol_token_expired = False
    if station and (station.device_token or "").strip():
        from plugins.YandexDevices.glagol_local import glagol_token_exp_unix, glagol_token_expired as _token_expired

        exp = glagol_token_exp_unix(station.device_token)
        if exp is not None:
            import datetime

            glagol_token_exp_hint = datetime.datetime.utcfromtimestamp(exp).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        glagol_token_expired = _token_expired(station.device_token)

    return render_template(
        "yandexdevices_station.html",
        id=station_id,
        form=form,
        glagol_url=_glagol_station_api_url(station_id),
        glagol_ws_status=glagol_ws_status,
        glagol_ws_status_url=glagol_ws_status_url,
        glagol_token_exp_hint=glagol_token_exp_hint,
        glagol_token_expired=glagol_token_expired,
    )