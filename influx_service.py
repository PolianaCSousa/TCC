from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS
import config

class InfluxService:
    def __init__(self):
        self.client = InfluxDBClient(
            url=config.INFLUXDB_URL,
            token=config.INFLUXDB_TOKEN,
            org=config.INFLUXDB_ORG,
        )
        self.write_client = self.client.write_api(write_options=SYNCHRONOUS)

    def write_data(self, tags : dict, fields : dict): #preciso passar measurements, tags e fields
        point = Point(config.INFLUXDB_MEASUREMENT)

        #add tags
        for key, value in tags.items():
            point.tag(key, value)

        #add fields
        for key, value in fields.items():
            point.field(key, value)

        self.write_client.write(
            bucket=config.INFLUXDB_BUCKET,
            org=config.INFLUXDB_ORG,
            record=point
        )

    def close(self):
        self.client.close()




