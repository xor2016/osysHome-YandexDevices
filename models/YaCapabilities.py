from app.database import Column, Model, SurrogatePK, db

class YaCapabilities(SurrogatePK, db.Model):
    __tablename__ = 'yadevices_capabilities'
    device_id = Column(db.Integer)
    title = Column(db.String(100))
    value = Column(db.String(255))
    read_only = Column(db.Integer)
    linked_object = Column(db.String(255))
    linked_property = Column(db.String(255))
    linked_method = Column(db.String(255))
    updated = Column(db.DateTime)