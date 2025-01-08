from flask_wtf import FlaskForm
from wtforms import StringField, SubmitField, IntegerField, BooleanField
from wtforms.validators import DataRequired, Optional
from wtforms.widgets import PasswordInput

# Определение класса формы
class SettingsForm(FlaskForm):
    get_data = BooleanField('Enable get device data', validators=[Optional()])
    update_period = IntegerField('Default update period device data (seconds)', validators=[Optional()])
    submit = SubmitField('Submit')