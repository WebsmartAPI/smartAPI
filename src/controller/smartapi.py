"""
    SmartAPI CRUD Validation and Refresh Operations

    Validation only:

        smartapi = SmartAPI(SmartAPI.VALIDATION_ONLY)
        smartapi.raw = rawbytes
        smartapi.validate()

    Add a document:

        smartapi = SmartAPI(url)
        smartapi.raw = rawbytes

        smartapi.username = username
        smartapi.slug = slug # optional

        smartapi.validate() # should be called before saving
        smartapi.save()

        smartapi.check() # populate uptime status
        smartapi.refresh() # refresh and populate refresh status, auto validate
        smartapi.save()

    Modify a document metadata:

        smartapi = SmartAPI.get(_id)
        smartapi.slug = newslug
        smartapi.save()

    Delete a document:

        smartapi = SmartAPI.get(_id)
        smartapi.delete()

"""
import logging
import string
from collections.abc import Mapping
from datetime import datetime, timezone
from warnings import warn

from model import ConsolidatedMetaKGDoc, MetaKGDoc, SmartAPIDoc
from utils import decoder, monitor
from utils.downloader import download
from utils.metakg.parser import MetaKGParser

from .base import AbstractWebEntity, APIMonitorStatus, APIRefreshStatus, OpenAPI, Swagger
from .exceptions import ConflictError, ControllerError

logger = logging.getLogger(__name__)

# TODO etag support


class Slug:
    """
    Optional secondary key for a DB entry.
    It must be a unique string or None.
    """

    def __get__(self, obj, objtype=None):
        if obj is None:  # class atrribute
            return self
        return getattr(obj, "_slug", None)

    def __set__(self, obj, value):
        self.validate(value)
        obj._slug = value

    def validate(self, value):
        if value is None:
            return  # this field is optional, okay to be None

        if not isinstance(value, str) or len(value) < 3:
            raise ValueError("Slug must be at least 3 characters.")

        if value != value.lower():
            raise ValueError("Slug must be in lowercase.")

        if value in ("www", "dev", "smart-api"):
            raise ValueError(f"Slug '{value}' not allowed.")

        _valid_chars = string.ascii_letters + string.digits + "-_"
        if not all(c in _valid_chars for c in value):
            raise ValueError("Slug contains invalid characters.")


class SmartAPI(AbstractWebEntity, Mapping):
    LOOKUP_FIELDS = ("slug", "username", "url")
    MODEL_CLASS = SmartAPIDoc
    INDEX = None   # set an alt. index name if not using the default one from MODEL_CLASS

    # SmartAPI.slug.validate(value: Union[str, NoneType]) -> None
    # smartapi.slug : Union[str, NoneType]
    slug = Slug()

    def __init__(self, url):
        super().__init__(url)
        self.username = None
        self.slug = None
        self.date_created = None
        self.last_updated = None
        self.has_metakg = None

        self.uptime = APIMonitorStatus(self)
        self.webdoc = APIRefreshStatus(self)

        self._raw = None

    @classmethod
    def get(cls, _id):
        obj = super().get(_id)

        obj.username = obj._doc._meta.username
        obj.slug = obj._doc._meta.slug
        obj.raw = decoder.decompress(obj._doc._raw)

        obj.date_created = obj._doc._meta.date_created
        obj.last_updated = obj._doc._meta.last_updated

        obj.has_metakg = obj._doc._meta.has_metakg

        obj.uptime = APIMonitorStatus(
            obj,
            (
                obj._doc._status.uptime_status,
                obj._doc._status.uptime_msg,
            ),
            obj._doc._status.uptime_ts,
        )

        obj.webdoc = APIRefreshStatus(
            obj,
            obj._doc._status.refresh_status,
            obj._doc._status.refresh_ts,
        )
        return obj

    @classmethod
    def get_tags(cls, field="info.contact.name"):
        return cls.MODEL_CLASS.aggregate(field, index=cls.INDEX)

    @classmethod
    def find(cls, val, field="slug"):
        return super().find(val, field=field)

    @classmethod
    def fetch_all_metakg(cls, include_trapi=True, report_errors=True):
        """Fetch metakg edges from all Translator APIs, and return as
        a generator of edges.
        """
        count_docs = cls.count()
        query_data = {"type": "term", "body": {"tags.name": "translator"}}
        all_apis = cls.get_all(size=count_docs, query_data=query_data)
        metakg_error_list = []
        for api in all_apis:
            logger.info("[%s]", api._doc.info.title)
            logger.info("SmartAPI ID: %s", api._id)
            metakg = api.get_metakg(include_trapi=include_trapi)
            if api.metakg_errors:
                api.metakg_errors.setdefault("_id", api._id)
                api.metakg_errors.setdefault("title", api._doc.info.title)
                api.metakg_errors.setdefault("url", "http://smart-api.info/registry?q=" + api._id)
                metakg_error_list.append(api.metakg_errors)
            yield from metakg  # each item is a metakg edge
        if report_errors and metakg_error_list:
            logger.error("=" * 50)
            logger.error("Found errors in %s APIs during metakg retrieval:", len(metakg_error_list))
            for error in metakg_error_list:
                logger.error(error)

    @classmethod
    def refresh_metakg(cls, include_trapi=True):
        """Fetch all metakg edges and saved to the ES index in bulk"""
        from elasticsearch.helpers import bulk
        from elasticsearch_dsl import connections

        es = connections.get_connection()
        edge_iterable = (
            MetaKGDoc(**edge).to_dict(include_meta=True) for edge in cls.fetch_all_metakg(include_trapi=include_trapi)
        )
        bulk(es, edge_iterable)

    @classmethod
    def edge_consolidation_build(cls):
        """Traverse through the MetaKG index and aggregate edges into groups based on their subject/predicate/object"""
        edge_dict = {}
        processed_edges = 0
        # loop through MetaKG index with ES scan method
        for edge in cls.get_all_via_scan(size=10000, index=MetaKGDoc.Index.name):
            # Set key which we group by: subject-predicate-object
            key = f'{edge["_source"]["subject"]}-{edge["_source"]["predicate"]}-{edge["_source"]["object"]}'
            # Set edge details
            edge_api = edge["_source"]["api"]

            # Add edge to the dictionary, merging API details if the edge already exists
            if key in edge_dict:
                if edge_api not in edge_dict[key]["api"]:
                    edge_dict[key]["api"].append(edge_api)
            else:
                edge_dict[key] = {
                    "_id": key,
                    "subject": edge["_source"]["subject"],
                    "object": edge["_source"]["object"],
                    "predicate": edge["_source"]["predicate"],
                    "api": [edge_api],
                    **{k: edge["_source"][k] for k in ["subject_prefix", "object_prefix"] if k in edge["_source"]}
                }

            processed_edges += 1

        for key in edge_dict:
            yield edge_dict[key]

        del edge_dict

    @classmethod
    def index_metakg_consolidation(cls):
        """Fetch all metakg edges in the ES index and consolidate edges(into another index)"""
        from elasticsearch.helpers import bulk
        from elasticsearch_dsl import connections

        es = connections.get_connection()
        edge_iterable = (
            ConsolidatedMetaKGDoc(**edge).to_dict(include_meta=True) for edge in cls.edge_consolidation_build()
        )
        bulk(es, edge_iterable)

    # Instance methods below which is specific to a given SmartAPI entity
    @property
    def raw(self):
        """
        Bytes that correspond to the URL.
        This object is a view of this field.
        """
        return self._raw

    @raw.setter
    def raw(self, value):
        if not value:
            raise ControllerError("Empty value.")
        try:
            self._data = decoder.to_dict(value)
        except (ValueError, TypeError) as err:
            raise ControllerError(str(err)) from err
        else:  # dict conversion success
            self._raw = value

        # update the timestamps
        self.last_updated = datetime.now(timezone.utc)
        if hasattr(self, "date_created") and not self.date_created:
            self.date_created = self.last_updated

    def check(self):
        doc = dict(self)
        doc["_id"] = self._id

        api = monitor.API(doc)
        api.check_api_status()  # blocking network operation
        status = api.get_api_status()
        self.uptime.update(status)
        return status

    def refresh(self, file=None):
        if file is None:  # blocking network operation
            file = download(self.url, raise_error=False)

        self.webdoc.update(file)
        return self.webdoc.status

    def save(self, force_save=True):
        # TODO DOCSTRING

        if not self.raw:
            raise ControllerError("No content.")

        if self.url is self.VALIDATION_ONLY:
            raise ControllerError("In validation-only mode.")

        if not self.username:
            raise ControllerError("Username is required.")
        if not self.last_updated:
            self.last_updated = datetime.now(timezone.utc)
            warn("Filling in date_updated with current time.")
        if not self.date_created:
            self.date_created = self.last_updated
            warn("Filling in date_created with current time.")

        if not isinstance(self.date_created, datetime):
            raise ControllerError("Invalid created time.")
        if not isinstance(self.last_updated, datetime):
            raise ControllerError("Invalid updated time.")
        if self.date_created > self.last_updated:
            raise ControllerError("Invalid timestamps.")

        # NOTE
        # if the slug of another document changed at this point
        # it's possible to have two documents with the same slug
        # registered. but it should be rare enough in reality.

        if self.slug:
            _id = self.find(self.slug)
            if _id and _id != self._id:  # another doc same slug.
                raise ConflictError("Slug is already registered.")

        # NOTE
        # why not enforce validation here?
        # we add additional constraints to the application from time to time
        # it's actually hard to retrospectively make sure all previously
        # submitted API document always meet our latest requirements

        _doc = self._validate_dispatch()
        _doc.transform()

        doc = self.MODEL_CLASS(**_doc)

        if self.uptime.status:
            doc._status.uptime_status = self.uptime.status[0]
            doc._status.uptime_msg = self.uptime.status[1]
        doc._status.uptime_ts = self.uptime.timestamp

        doc._status.refresh_status = self.webdoc.status
        doc._status.refresh_ts = self.webdoc.timestamp

        doc._raw = decoder.compress(self.raw)

        doc.meta.id = self._id
        doc._meta.url = self.url
        doc._meta.username = self.username
        doc._meta.date_created = self.date_created
        doc._meta.last_updated = self.last_updated
        doc._meta.slug = self.slug
        doc._meta.has_metakg = self.has_metakg
        doc.save(skip_empty=False, index=self.INDEX)

        return self._id

    @property
    def version(self):
        """
        API specification format version name.
        This field decides how it is saved in ES.
        """
        if "openapi" in self._data:
            return "openapi"
        if "swagger" in self._data:
            return "swagger"
        return None

    def _validate_dispatch(self):
        if not self.version:
            raise ControllerError("Unknown version.")

        if self.version == "openapi":
            doc = OpenAPI(self._data)
        else:  # then it must be swagger
            doc = Swagger(self._data)

        return doc

    def has_tags(self, *tags):
        """return True if an SmartAPI contains all given tags"""
        _tag_set = set([_tag.name for _tag in self._doc.tags])
        return len(set(tags) - _tag_set) == 0

    @property
    def is_trapi(self):
        """return True if a TRAPI"""
        return self.has_tags("trapi", "translator")

    def get_metakg(self, include_trapi=True, metadata_url=False):
        if metadata_url:
            data_id = decoder.get_id(self.url)
            doc = self.get(data_id)
            self._doc = doc._doc
            raw_metadata = decoder.to_dict(decoder.decompress(doc._doc._raw))
        else:
            raw_metadata = decoder.to_dict(decoder.decompress(self._doc._raw))
        mkg_parser = MetaKGParser()
        extra_data = {"id": self._id, "url": self.url}
        self.metakg_errors = None  # reset metakg_errors
        if self.is_trapi:
            metakg = mkg_parser.get_TRAPI_metadatas(raw_metadata, extra_data) if include_trapi else []
        else:
            metakg = mkg_parser.get_non_TRAPI_metadatas(raw_metadata, extra_data)
        if mkg_parser.metakg_errors:
            # hold metakg_errors for later use
            self.metakg_errors = mkg_parser.metakg_errors
        return metakg
