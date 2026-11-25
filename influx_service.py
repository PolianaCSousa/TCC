import logging
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS
import config

logger = logging.getLogger(__name__)


class InfluxService:
    def __init__(self):
        self.client = InfluxDBClient(
            url=config.INFLUXDB_URL,
            token=config.INFLUXDB_TOKEN,
            org=config.INFLUXDB_ORG,
        )
        self.write_client = self.client.write_api(write_options=SYNCHRONOUS)

    def write_data(self, tags : dict, fields : dict, measurement : str | None = None):
        point = Point(measurement or config.INFLUXDB_MEASUREMENT)

        # tags are always strings in influx
        for key, value in tags.items():
            if value is None:
                continue
            point.tag(key, str(value))

        # every field as float to avoid type conflicts
        written_fields = 0
        for key, value in fields.items():
            if value is None:  # unmeasured metrics are not written
                continue
            point.field(key, float(value))
            written_fields += 1

        if written_fields == 0:  # influx rejects a point with no fields
            logger.warning("Nenhum field preenchido, nada foi escrito no InfluxDB.")
            return

        self.write_client.write(
            bucket=config.INFLUXDB_BUCKET,
            org=config.INFLUXDB_ORG,
            record=point
        )

    def close(self):
        self.client.close()
