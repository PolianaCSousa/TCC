import aiohttp
import base64
import json


class KuboClient:
    def __init__(self, api_url="http://127.0.0.1:5001"):
        self.api_url = api_url
        self.session = None

    async def start(self):
        self.session = aiohttp.ClientSession()

    async def id(self):
        async with self.session.post(self.api_url + "/api/v0/id") as response:
            data = await response.json()
            return data["ID"]

    async def pubsub_pub(self, topic, data):
        encoded_topic = self.encode_topic(topic)
        params = { "arg": encoded_topic }
        form = aiohttp.FormData()
        form.add_field("file", data.encode("utf-8"), filename="file")

        async with self.session.post(
            self.api_url + "/api/v0/pubsub/pub",
            params=params,
            data=form
        ) as response:
            print(response)


    async def pubsub_sub(self, topic):
        encoded_topic = self.encode_topic(topic)
        params = { "arg": encoded_topic }
        async with self.session.post(
            self.api_url + "/api/v0/pubsub/sub",
            params=params,
        ) as response:
            async for line in response.content:
                line = line.strip()
                if not line:
                    continue
                message = json.loads(line)
                message["data"] = self.decode_data(message["data"])
                yield message
    


    def encode_topic(self, topic):
        encoded = base64.urlsafe_b64encode(topic.encode("utf-8")).decode("utf-8")
        without_padding = encoded.rstrip("=")
        encoded_topic = "u" + without_padding

        return encoded_topic

    def decode_data(self, data):
        without_prefix = data[1:]
        with_padding = without_prefix + "=" * (-len(without_prefix) % 4)
        decoded_data = base64.urlsafe_b64decode(with_padding).decode("utf-8")

        return decoded_data


    async def close(self):
        await self.session.close()