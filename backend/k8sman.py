""" K8s support"""

import os
import datetime
import json

from kubernetes_asyncio import client, config


# ============================================================================
DEFAULT_NAMESPACE = os.environ.get("CRAWLER_NAMESPACE") or "crawlers"

DEFAULT_NO_SCHEDULE = "* * 31 2 *"


# ============================================================================
class K8SManager:
    # pylint: disable=too-many-instance-attributes,too-many-locals,too-many-arguments
    """K8SManager, manager creation of k8s resources from crawl api requests"""

    def __init__(self, namespace=DEFAULT_NAMESPACE):
        config.load_incluster_config()

        self.core_api = client.CoreV1Api()
        self.batch_api = client.BatchV1Api()
        self.batch_beta_api = client.BatchV1beta1Api()

        self.namespace = namespace

        self.crawler_image = os.environ.get("CRAWLER_IMAGE")
        self.crawler_image_pull_policy = "IfNotPresent"

        # loop = asyncio.get_running_loop()
        # loop.create_task(self.watch_job_done())

    async def validate_crawl_complete(self, crawlcomplete):
        """Ensure the crawlcomplete data is valid (job exists and user matches)
        Fill in additional details about the crawl"""
        job = await self.batch_api.read_namespaced_job(
            name=crawlcomplete.id, namespace=self.namespace
        )

        if not job or job.metadata.labels["btrix.user"] != crawlcomplete.user:
            return False

        # job.metadata.annotations = {
        #    "crawl.size": str(crawlcomplete.size),
        #    "crawl.filename": crawlcomplete.filename,
        #    "crawl.hash": crawlcomplete.hash
        # }

        # await self.batch_api.patch_namespaced_job(
        #    name=crawlcomplete.id, namespace=self.namespace, body=job
        # )

        crawlcomplete.started = job.status.start_time.replace(tzinfo=None)
        crawlcomplete.aid = job.metadata.labels["btrix.archive"]
        crawlcomplete.cid = job.metadata.labels["btrix.crawlconfig"]
        crawlcomplete.finished = datetime.datetime.utcnow().replace(
            microsecond=0, tzinfo=None
        )
        return True

    async def add_crawl_config(
        self,
        userid: str,
        aid: str,
        storage,
        crawlconfig,
        extra_crawl_params: list = None,
    ):
        """add new crawl as cron job, store crawl config in configmap"""
        cid = str(crawlconfig.id)

        labels = {
            "btrix.user": userid,
            "btrix.archive": aid,
            "btrix.crawlconfig": cid,
        }

        # Create Config Map
        config_map = self._create_config_map(crawlconfig, labels)

        await self.core_api.create_namespaced_config_map(
            namespace=self.namespace, body=config_map
        )

        # Create Secret
        endpoint_with_coll_url = os.path.join(
            storage.endpoint_url, "collections", crawlconfig.config.collection + "/"
        )

        crawl_secret = client.V1Secret(
            metadata={
                "name": f"crawl-secret-{cid}",
                "namespace": self.namespace,
                "labels": labels,
            },
            string_data={
                "STORE_USER": userid,
                "STORE_ARCHIVE": aid,
                "STORE_ENDPOINT_URL": endpoint_with_coll_url,
                "STORE_ACCESS_KEY": storage.access_key,
                "STORE_SECRET_KEY": storage.secret_key,
                "WEBHOOK_URL": "http://browsertrix-cloud.default:8000/crawls/done",
            },
        )

        await self.core_api.create_namespaced_secret(
            namespace=self.namespace, body=crawl_secret
        )

        # Create Cron Job

        suspend, schedule, run_now = self._get_schedule_suspend_run_now(crawlconfig)

        extra_crawl_params = extra_crawl_params or []

        job_template = self._get_job_template(cid, labels, extra_crawl_params)

        spec = client.V1beta1CronJobSpec(
            schedule=schedule,
            suspend=suspend,
            concurrency_policy="Forbid",
            successful_jobs_history_limit=2,
            failed_jobs_history_limit=3,
            job_template=job_template,
        )

        cron_job = client.V1beta1CronJob(
            metadata={
                "name": f"scheduled-crawl-{cid}",
                "namespace": self.namespace,
                "labels": labels,
            },
            spec=spec,
        )

        cron_job = await self.batch_beta_api.create_namespaced_cron_job(
            namespace=self.namespace, body=cron_job
        )

        # Run Job Now
        if run_now:
            await self._create_run_now_job(cron_job)

        return cron_job

    async def update_crawl_config(self, crawlconfig):
        """ Update existing crawl config """

        cid = crawlconfig.id

        cron_jobs = await self.batch_beta_api.list_namespaced_cron_job(
            namespace=self.namespace, label_selector=f"btrix.crawlconfig={cid}"
        )

        if len(cron_jobs.items) != 1:
            return

        cron_job = cron_jobs.items[0]

        if crawlconfig.archive != cron_job.metadata.labels["btrix.archive"]:
            print("wrong archive")
            return

        labels = {
            "btrix.user": cron_job.metadata.labels["btrix.user"],
            "btrix.archive": crawlconfig.archive,
            "btrix.crawlconfig": cid,
        }

        # Update Config Map
        config_map = self._create_config_map(crawlconfig, labels)

        await self.core_api.patch_namespaced_config_map(
            name=f"crawl-config-{cid}", namespace=self.namespace, body=config_map
        )

        # Update CronJob, if needed
        suspend, schedule, run_now = self._get_schedule_suspend_run_now(crawlconfig)

        changed = False

        if schedule != cron_job.spec.schedule:
            cron_job.spec.schedule = schedule
            changed = True

        if suspend != cron_job.spec.suspend:
            cron_job.spec.suspend = suspend
            changed = True

        if changed:
            await self.batch_beta_api.patch_namespaced_cron_job(
                name=cron_job.metadata.name, namespace=self.namespace, body=cron_job
            )

        # Run Job Now
        if run_now:
            await self._create_run_now_job(cron_job)

    def _create_config_map(self, crawlconfig, labels):
        """ Create Config Map based on CrawlConfig + labels """
        config_map = client.V1ConfigMap(
            metadata={
                "name": f"crawl-config-{crawlconfig.id}",
                "namespace": self.namespace,
                "labels": labels,
            },
            data={"crawl-config.json": json.dumps(crawlconfig.config.dict())},
        )

        return config_map

    #pylint: disable=no-self-use
    def _get_schedule_suspend_run_now(self, crawlconfig):
        """ get schedule/suspend/run_now data based on crawlconfig """

        # Create Cron Job
        suspend = False
        schedule = crawlconfig.schedule

        if not schedule:
            schedule = DEFAULT_NO_SCHEDULE
            suspend = True

        run_now = False
        if crawlconfig.runNow:
            run_now = True

        return suspend, schedule, run_now

    async def delete_crawl_configs_for_archive(self, archive):
        """Delete all crawl configs for given archive"""
        return await self._delete_crawl_configs(f"btrix.archive={archive}")

    async def delete_crawl_config_by_id(self, cid):
        """Delete all crawl configs by id"""
        return await self._delete_crawl_configs(f"btrix.crawlconfig={cid}")

    async def _delete_crawl_configs(self, label):
        """Delete Crawl Cron Job and all dependent resources, including configmap and secrets"""

        await self.batch_beta_api.delete_collection_namespaced_cron_job(
            namespace=self.namespace,
            label_selector=label,
            propagation_policy="Foreground",
        )

        await self.core_api.delete_collection_namespaced_secret(
            namespace=self.namespace,
            label_selector=label,
            propagation_policy="Foreground",
        )

        await self.core_api.delete_collection_namespaced_config_map(
            namespace=self.namespace,
            label_selector=label,
            propagation_policy="Foreground",
        )

    async def run_crawl_config(self, cid):
        """ Run crawl job for cron job based on specified crawlconfig id (cid) """
        cron_jobs = await self.batch_beta_api.list_namespaced_cron_job(
            namespace=self.namespace, label_selector=f"btrix.crawlconfig={cid}"
        )

        res = await self._create_run_now_job(cron_jobs.items[0])
        return res.metadata.name

    async def _create_run_now_job(self, cron_job):
        """Create new job from cron job to run instantly"""
        annotations = {}
        annotations["cronjob.kubernetes.io/instantiate"] = "manual"

        owner_ref = client.V1OwnerReference(
            kind="CronJob",
            name=cron_job.metadata.name,
            block_owner_deletion=True,
            controller=True,
            uid=cron_job.metadata.uid,
            api_version="batch/v1beta1",
        )

        ts_now = datetime.datetime.utcnow().strftime("%Y%m%d%H%M%S")
        name = f"crawl-now-{ts_now}-{cron_job.metadata.labels['btrix.crawlconfig']}"

        object_meta = client.V1ObjectMeta(
            name=name,
            annotations=annotations,
            labels=cron_job.metadata.labels,
            owner_references=[owner_ref],
        )

        job = client.V1Job(
            kind="Job",
            api_version="batch/v1",
            metadata=object_meta,
            spec=cron_job.spec.job_template.spec,
        )

        return await self.batch_api.create_namespaced_job(
            body=job, namespace=self.namespace
        )

    def _get_job_template(self, uid, labels, extra_crawl_params):
        """Return crawl job template for crawl job, including labels, adding optiona crawl params"""

        command = ["crawl", "--config", "/tmp/crawl-config.json"]

        if extra_crawl_params:
            command += extra_crawl_params

        requests_memory = "256M"
        limit_memory = "1G"

        requests_cpu = "120m"
        limit_cpu = "1000m"

        resources = {
            "limits": {
                "cpu": limit_cpu,
                "memory": limit_memory,
            },
            "requests": {
                "cpu": requests_cpu,
                "memory": requests_memory,
            },
        }

        return {
            "spec": {
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        "containers": [
                            {
                                "name": "crawler",
                                "image": self.crawler_image,
                                "imagePullPolicy": "Never",
                                "command": command,
                                "volumeMounts": [
                                    {
                                        "name": "crawl-config",
                                        "mountPath": "/tmp/crawl-config.json",
                                        "subPath": "crawl-config.json",
                                        "readOnly": True,
                                    }
                                ],
                                "envFrom": [
                                    {"secretRef": {"name": f"crawl-secret-{uid}"}}
                                ],
                                "env": [
                                    {
                                        "name": "CRAWL_ID",
                                        "valueFrom": {
                                            "fieldRef": {
                                                "fieldPath": "metadata.labels['job-name']"
                                            }
                                        },
                                    }
                                ],
                                "resources": resources,
                            }
                        ],
                        "volumes": [
                            {
                                "name": "crawl-config",
                                "configMap": {
                                    "name": f"crawl-config-{uid}",
                                    "items": [
                                        {
                                            "key": "crawl-config.json",
                                            "path": "crawl-config.json",
                                        }
                                    ],
                                },
                            }
                        ],
                        "restartPolicy": "OnFailure",
                    },
                }
            }
        }
