from flask import redirect, render_template
from flask_wtf import FlaskForm
from wtforms import StringField, SubmitField, BooleanField, SelectField, IntegerField, RadioField
from wtforms.validators import DataRequired, Optional
from ..models.YaDevices import YaDevices
from app.database import db
from app.core.lib.object import getObjectsByClass

# Определение класса формы
class DeviceForm(FlaskForm):
    title = StringField('Title', validators=[DataRequired()])
    device_type = StringField('Device type')
    iot_id = StringField("IOT id")
    icon = StringField("Icon")
    submit = SubmitField('Submit')

def editDevice(request):
    device_id = request.args.get("device", None)
    device = YaDevices.get_by_id(device_id)
    form = DeviceForm(obj=device)  # Передаем объект в форму для редактирования

    if form.validate_on_submit():
        if device_id:
            form.populate_obj(device)  # Обновляем значения объекта данными из формы
            db.session.commit()  # Сохраняем изменения в базе данных
            return redirect("YandexDevices?tab=devices")

    return render_template('yandexdevices_device.html', id=device_id, form=form)