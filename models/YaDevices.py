from app.database import Column, Model, SurrogatePK, db

class YaDevices(SurrogatePK, db.Model):
    __tablename__ = 'yadevices'
    title = Column(db.String(100))
    device_type = Column(db.String(255))
    icon = Column(db.Text)
    iot_id = Column(db.String(255))
    updated = Column(db.DateTime) 
