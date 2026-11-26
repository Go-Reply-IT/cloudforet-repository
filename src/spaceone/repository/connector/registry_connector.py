import abc
import requests
import logging
import boto3
from distutils.version import StrictVersion
import os
from urllib.parse import urljoin, urlparse



from spaceone.core.connector import BaseConnector
from spaceone.repository.error import *

__all__ = ['DockerHubConnector', 'AWSPrivateECRConnector', 'HarborConnector']

_LOGGER = logging.getLogger(__name__)
_DEFAULT_PAGE_SIZE = 10


class RegistryConnector(BaseConnector):

    @abc.abstractmethod
    def get_tags(self, registry_url, image, config):
        pass


class DockerHubConnector(RegistryConnector):

    _ARTIFACT_REGISTRY_HOST = "europe-west8-docker.pkg.dev"

    _ARTIFACT_REGISTRY_PREFIX = (
        "goreply-msp-mgmt-prj/cloudforet-plugins/"
    )

    _METADATA_TOKEN_URL = (
        "http://169.254.169.254/computeMetadata/v1/"
        "instance/service-accounts/default/token"
    )

    def get_tags(self, registry_url, image, config):

        # Google Artifact Registry
        artifact_prefix = (
            self._ARTIFACT_REGISTRY_HOST + "/"
        )

        if image.startswith(artifact_prefix):
            image_path = image[len(artifact_prefix):]

            return self._get_artifact_registry_tags(
                image_path
            )

        # Private Docker Hub
        private_namespace = os.getenv(
            "DOCKERHUB_PRIVATE_NAMESPACE", ""
        )

        if (
            private_namespace
            and image.startswith(private_namespace + "/")
        ):
            return self._get_private_dockerhub_tags(image)

        # Public Docker Hub: original behavior
        return self._get_public_dockerhub_tags(
            registry_url,
            image
        )

    def _get_public_dockerhub_tags(
        self, registry_url, image
    ):

        url = (
            f"https://{registry_url}"
            f"/v2/repositories/{image}/tags"
        )

        response = requests.get(url, timeout=30)

        if response.status_code == 200:
            results = response.json().get("results", [])
            return [tag["name"] for tag in results]

        raise ERROR_NO_IMAGE_IN_REGISTRY(
            registry_type="DOCKER_HUB",
            image=image
        )

    def _get_private_dockerhub_tags(self, image):

        username = os.getenv("DOCKERHUB_USERNAME")
        password = os.getenv("DOCKERHUB_TOKEN")

        if not username or not password:
            raise ERROR_CONNECTOR_CONFIGURATION(
                connector="DockerHubConnector"
            )

        try:

            token_response = requests.get(
                "https://auth.docker.io/token",
                params={
                    "service": "registry.docker.io",
                    "scope": f"repository:{image}:pull"
                },
                auth=(username, password),
                timeout=30,
                allow_redirects=False
            )

            token_response.raise_for_status()

            token_data = token_response.json()

            token = (
                token_data.get("token")
                or token_data.get("access_token")
            )

            if not token:
                raise ValueError("Missing registry token")

            host = "registry-1.docker.io"

            url = (
                f"https://{host}"
                f"/v2/{image}/tags/list?n=100"
            )

            return self._list_registry_tags(
                url=url,
                host=host,
                headers={
                    "Authorization": f"Bearer {token}"
                }
            )

        except (requests.RequestException, ValueError):

            _LOGGER.error(
                "Private Docker Hub discovery failed for %s",
                image
            )

            raise ERROR_NO_IMAGE_IN_REGISTRY(
                registry_type="DOCKER_HUB",
                image=image
            )

    def _get_artifact_registry_tags(self, image_path):

        host = self._ARTIFACT_REGISTRY_HOST
        full_image = f"{host}/{image_path}"

        if not image_path.startswith(
            self._ARTIFACT_REGISTRY_PREFIX
        ):
            raise ERROR_NO_IMAGE_IN_REGISTRY(
                registry_type="DOCKER_HUB",
                image=full_image
            )

        try:

            access_token = self._get_gcp_access_token()

            url = (
                f"https://{host}"
                f"/v2/{image_path}/tags/list?n=100"
            )

            return self._list_registry_tags(
                url=url,
                host=host,
                headers={
                    "Authorization": f"Bearer {access_token}"
                }
            )

        except (requests.RequestException, ValueError):

            _LOGGER.error(
                "Artifact Registry discovery failed for %s",
                full_image
            )

            raise ERROR_NO_IMAGE_IN_REGISTRY(
                registry_type="DOCKER_HUB",
                image=full_image
            )

    def _get_gcp_access_token(self):

        # Optional short-lived token for local testing.
        override = os.getenv("GCP_ACCESS_TOKEN")

        if override:
            return override

        # Production: use the GCE metadata server.
        with requests.Session() as session:

            session.trust_env = False

            response = session.get(
                self._METADATA_TOKEN_URL,
                headers={
                    "Metadata-Flavor": "Google"
                },
                timeout=10,
                allow_redirects=False
            )

            response.raise_for_status()

            if response.status_code != 200:
                raise ValueError("Invalid metadata response")

            token = response.json().get("access_token")

            if not token:
                raise ValueError("Missing GCP access token")

            return token

    @staticmethod
    def _list_registry_tags(url, host, headers):

        tags = []
        visited = set()

        while url:

            parsed = urlparse(url)

            if (
                parsed.scheme != "https"
                or parsed.netloc != host
            ):
                raise ValueError(
                    "Unexpected registry pagination URL"
                )

            if url in visited:
                raise ValueError(
                    "Registry pagination loop detected"
                )

            visited.add(url)

            response = requests.get(
                url,
                headers=headers,
                timeout=30,
                allow_redirects=False
            )

            response.raise_for_status()

            if response.status_code != 200:
                raise ValueError(
                    "Unexpected registry response"
                )

            tags.extend(
                response.json().get("tags") or []
            )

            next_url = response.links.get(
                "next", {}
            ).get("url")

            url = (
                urljoin(url, next_url)
                if next_url else None
            )

        return sorted(set(tags), reverse=True)

class HarborConnector(RegistryConnector):

    def get_tags(self, registry_url, image, config):
        base_url = self.config.get('base_url')
        verify = self.config.get('verify', True)

        headers = {
            'authorization': f'Basic {self.config["token"]}'
        }

        url = f'{base_url}/v2/{image}/tags/list'

        response = requests.get(url, headers=headers, verify=verify)

        if response.status_code == 200:
            image_tags = response.json().get('tags', [])
            image_tags.sort(key=StrictVersion, reverse=True)
            return image_tags
        else:
            _LOGGER.error(f'[get_tags] request error: {response.json()}')

        raise ERROR_NO_IMAGE_IN_REGISTRY(registry_type='HARBOR', image=image)


class AWSPrivateECRConnector(RegistryConnector):
    _DEFAULT_REGION_NAME = 'ap-northeast-2'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        aws_access_key_id = self.config.get('aws_access_key_id')
        aws_secret_access_key = self.config.get('aws_secret_access_key')

        if not all([aws_access_key_id, aws_secret_access_key]):
            raise ERROR_CONNECTOR_CONFIGURATION(connector='AWSPrivateECRConnector')

        self.client = boto3.client('ecr',
                                   aws_access_key_id=aws_access_key_id,
                                   aws_secret_access_key=aws_secret_access_key,
                                   region_name=self._DEFAULT_REGION_NAME)

    def get_tags(self, registry_url, image, config):

        account_id = config.get('account_id')

        try:
            response = self.client.describe_images(repositoryName=image, registryId=account_id)

            image_tags = []
            images_info = response.get('imageDetails', [])
            sorted_images_info = sorted(images_info, key=lambda k: k['imagePushedAt'], reverse=True)
            for image_info in sorted_images_info:
                image_tags.extend(image_info['imageTags'])

            return image_tags

        except Exception as e:
            _LOGGER.error(f'[get_tags] boto3 describe_image_tags error: {e}')
            raise ERROR_NO_IMAGE_IN_REGISTRY(registry_type='AWS_PRIVATE_ECR', image=image)


if __name__ == '__main__':
    docker_hub_conn = DockerHubConnector()
    tags = docker_hub_conn.get_tags('registry.hub.docker.com', 'spaceone/repository', {})
    print(tags)
