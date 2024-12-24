from app.database import Column, Model, SurrogatePK, db

class YaStation(SurrogatePK, db.Model):
    __tablename__ = 'yastation'
    title = Column(db.String(100))
    platform = Column(db.String(255))
    icon = Column(db.Text)
    ip = Column(db.String(25))
    min_level = Column(db.String(100))
    station_id = Column(db.String(255))
    iot_id = Column(db.String(255))
    device_token = Column(db.String(255))
    screen_capable = Column(db.Integer())
    screen_present = Column(db.Integer())
    online = Column(db.Integer())
    tts_scenario = Column(db.String(100))
    tts = Column(db.Integer())
    updated = Column(db.DateTime) 


