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

    def write_data(self, tags : dict, fields : dict): #preciso passar measurements, tags e fields
        point = Point(config.INFLUXDB_MEASUREMENT)

        #add tags - tag é sempre string no influx
        for key, value in tags.items():
            if value is None:
                continue
            point.tag(key, str(value))

        #add fields - float em todos pra não dar conflito de tipo quando a métrica vem inteira (ex: package_loss = 0)
        written_fields = 0
        for key, value in fields.items():
            if value is None:  # métrica que não foi medida nessa rodada não vai pro banco
                continue
            point.field(key, float(value))
            written_fields += 1

        if written_fields == 0:  # ponto sem field nenhum é rejeitado pelo influx
            logger.warning("Nenhum field preenchido, nada foi escrito no InfluxDB.")
            return

        self.write_client.write(
            bucket=config.INFLUXDB_BUCKET,
            org=config.INFLUXDB_ORG,
            record=point
        )

    def close(self):
        self.client.close()
