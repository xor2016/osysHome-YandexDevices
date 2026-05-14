from flask import current_app, redirect, render_template, url_for
from flask_wtf import FlaskForm
from wtforms import StringField, SubmitField, BooleanField, SelectField, IntegerField, RadioField
from wtforms.validators import DataRequired, Optional
from ..models.YaStation import YaStation
from app.database import db
from app.core.lib.object import getObjectsByClass

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


def editStation(request):
    station_id = request.args.get("station", None)
    station = YaStation.get_by_id(station_id)
    form = StationForm(obj=station)  # Передаем объект в форму для редактирования
    
    if form.validate_on_submit():
        if station_id:
            form.populate_obj(station)  # Обновляем значения объекта данными из формы
            db.session.commit()  # Сохраняем изменения в базе данных
            return redirect("YandexDevices")
    
    return render_template(
        "yandexdevices_station.html",
        id=station_id,
        form=form,
        glagol_url=_glagol_station_api_url(station_id),
    )