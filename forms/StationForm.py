from flask import redirect, render_template
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
    tts = RadioField('TTS', choices=[(0, 'No'), (1, 'Local (not work)'), (2, 'Cloud')],default=0)
    min_level = StringField("Min level SAY")
    device_token = StringField("Token")
    submit = SubmitField('Submit')

def editStation(request):
    station_id = request.args.get("station", None)
    station = YaStation.get_by_id(station_id)
    form = StationForm(obj=station)  # Передаем объект в форму для редактирования
    
    if form.validate_on_submit():
        if station_id:
            form.populate_obj(station)  # Обновляем значения объекта данными из формы
            db.session.commit()  # Сохраняем изменения в базе данных
            return redirect("YandexDevices")
    
    return render_template('yandexdevices_station.html', id=station_id, form=form)