# -*- coding: utf-8 -*-
#
# Copyright (C) 2015-2019 Bitergia
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA 02111-1307, USA.
#
# Authors:
#   Alvaro del Castillo San Felix <acs@bitergia.com>
#

import inspect
import logging
import pickle

import redis
from elasticsearch import Elasticsearch

from datetime import datetime
from dateutil import parser

from arthur.common import Q_STORAGE_ITEMS
from perceval.backend import find_signature_parameters, Archive
from grimoirelab_toolkit.datetime import datetime_utcnow

from .elastic_mapping import Mapping as BaseMapping
from .elastic_items import ElasticItems
from .enriched.sortinghat_gelk import SortingHat
from .enriched.utils import get_last_enrich, grimoire_con, get_current_date_minus_hours
from .utils import get_elastic
from .utils import get_connectors, get_connector_from_name

IDENTITIES_INDEX = "grimoirelab-identities-cache"

logger = logging.getLogger(__name__)

requests_ses = grimoire_con()

arthur_items = {}  # Hash with tag list with all items collected from arthur queue


def feed_arthur():
    """ Feed Ocean with backend data collected from arthur redis queue"""

    logger.info("Collecting items from redis queue")

    db_url = 'redis://localhost/8'

    conn = redis.StrictRedis.from_url(db_url)
    logger.debug("Redis connection stablished with %s.", db_url)

    # Get and remove queued items in an atomic transaction
    pipe = conn.pipeline()
    pipe.lrange(Q_STORAGE_ITEMS, 0, -1)
    pipe.ltrim(Q_STORAGE_ITEMS, 1, 0)
    items = pipe.execute()[0]

    for item in items:
        arthur_item = pickle.loads(item)
        if arthur_item['tag'] not in arthur_items:
            arthur_items[arthur_item['tag']] = []
        arthur_items[arthur_item['tag']].append(arthur_item)

    for tag in arthur_items:
        logger.debug("Items for %s: %i", tag, len(arthur_items[tag]))


def feed_backend_arthur(backend_name, backend_params):
    """ Feed Ocean with backend data collected from arthur redis queue"""

    # Always get pending items from arthur for all data sources
    feed_arthur()

    logger.debug("Items available for %s", arthur_items.keys())

    # Get only the items for the backend
    if not get_connector_from_name(backend_name):
        raise RuntimeError("Unknown backend %s" % backend_name)
    connector = get_connector_from_name(backend_name)
    klass = connector[3]  # BackendCmd for the connector

    backend_cmd = init_backend(klass(*backend_params))

    tag = backend_cmd.backend.tag
    logger.debug("Getting items for %s.", tag)

    if tag in arthur_items:
        logger.debug("Found items for %s.", tag)
        for item in arthur_items[tag]:
            yield item


def feed_backend(url, clean, fetch_archive, backend_name, backend_params,
                 es_index=None, es_index_enrich=None, project=None, arthur=False,
                 es_aliases=None):
    """ Feed Ocean with backend data """

    backend = None
    repo = {'backend_name': backend_name, 'backend_params': backend_params}  # repository data to be stored in conf

    if es_index:
        clean = False  # don't remove index, it could be shared

    if not get_connector_from_name(backend_name):
        raise RuntimeError("Unknown backend %s" % backend_name)
    connector = get_connector_from_name(backend_name)
    klass = connector[3]  # BackendCmd for the connector

    try:
        logger.info("Feeding Ocean from %s (%s)", backend_name, es_index)

        if not es_index:
            logger.error("Raw index not defined for %s", backend_name)

        repo['repo_update_start'] = datetime.now().isoformat()

        # perceval backends fetch params
        offset = None
        from_date = None
        category = None
        latest_items = None
        filter_classified = None

        backend_cmd = klass(*backend_params)

        parsed_args = vars(backend_cmd.parsed_args)
        init_args = find_signature_parameters(backend_cmd.BACKEND,
                                              parsed_args)

        if backend_cmd.archive_manager and fetch_archive:
            archive = Archive(parsed_args['archive_path'])
        else:
            archive = backend_cmd.archive_manager.create_archive() if backend_cmd.archive_manager else None

        init_args['archive'] = archive
        backend_cmd.backend = backend_cmd.BACKEND(**init_args)
        backend = backend_cmd.backend

        ocean_backend = connector[1](backend, fetch_archive=fetch_archive, project=project)
        elastic_ocean = get_elastic(url, es_index, clean, ocean_backend, es_aliases)
        ocean_backend.set_elastic(elastic_ocean)

        if fetch_archive:
            signature = inspect.signature(backend.fetch_from_archive)
        else:
            signature = inspect.signature(backend.fetch)

        if 'from_date' in signature.parameters:
            try:
                # Support perceval pre and post BackendCommand refactoring
                from_date = backend_cmd.from_date
            except AttributeError:
                from_date = backend_cmd.parsed_args.from_date

        if 'offset' in signature.parameters:
            try:
                offset = backend_cmd.offset
            except AttributeError:
                offset = backend_cmd.parsed_args.offset

        if 'category' in signature.parameters:
            try:
                category = backend_cmd.category
            except AttributeError:
                try:
                    category = backend_cmd.parsed_args.category
                except AttributeError:
                    pass

        if 'filter_classified' in signature.parameters:
            try:
                filter_classified = backend_cmd.parsed_args.filter_classified
            except AttributeError:
                pass

        if 'latest_items' in signature.parameters:
            try:
                latest_items = backend_cmd.latest_items
            except AttributeError:
                latest_items = backend_cmd.parsed_args.latest_items

        # fetch params support
        if arthur:
            # If using arthur just provide the items generator to be used
            # to collect the items and upload to Elasticsearch
            aitems = feed_backend_arthur(backend_name, backend_params)
            ocean_backend.feed(arthur_items=aitems)
        else:
            params = {}
            if latest_items:
                params['latest_items'] = latest_items
            if category:
                params['category'] = category
            if filter_classified:
                params['filter_classified'] = filter_classified
            if from_date and (from_date.replace(tzinfo=None) != parser.parse("1970-01-01")):
                params['from_date'] = from_date
            if offset:
                params['from_offset'] = offset

            ocean_backend.feed(**params)

    except Exception as ex:
        if backend:
            logger.error("Error feeding ocean from %s (%s): %s", backend_name, backend.origin, ex, exc_info=True)
        else:
            logger.error("Error feeding ocean %s", ex, exc_info=True)

    logger.info("Done %s ", backend_name)


def get_items_from_uuid(uuid, enrich_backend, ocean_backend):
    """ Get all items that include uuid """

    # logger.debug("Getting items for merged uuid %s "  % (uuid))

    uuid_fields = enrich_backend.get_fields_uuid()

    terms = ""  # all terms with uuids in the enriched item

    for field in uuid_fields:
        terms += """
         {"term": {
           "%s": {
              "value": "%s"
           }
         }}
         """ % (field, uuid)
        terms += ","

    terms = terms[:-1]  # remove last , for last item

    query = """
    {"query": { "bool": { "should": [%s] }}}
    """ % (terms)

    url_search = enrich_backend.elastic.index_url + "/_search"
    url_search += "?size=1000"  # TODO get all items

    r = requests_ses.post(url_search, data=query)

    eitems = r.json()['hits']['hits']

    if len(eitems) == 0:
        # logger.warning("No enriched items found for uuid: %s " % (uuid))
        return []

    items_ids = []

    for eitem in eitems:
        item_id = enrich_backend.get_item_id(eitem)
        # For one item several eitems could be generated
        if item_id not in items_ids:
            items_ids.append(item_id)

    # Time to get the items
    logger.debug("Items to be renriched for merged uuids: %s" % (",".join(items_ids)))

    url_mget = ocean_backend.elastic.index_url + "/_mget"

    items_ids_query = ""

    for item_id in items_ids:
        items_ids_query += '{"_id" : "%s"}' % (item_id)
        items_ids_query += ","
    items_ids_query = items_ids_query[:-1]  # remove last , for last item

    query = '{"docs" : [%s]}' % (items_ids_query)
    r = requests_ses.post(url_mget, data=query)

    res_items = r.json()['docs']

    items = []
    for res_item in res_items:
        if res_item['found']:
            items.append(res_item["_source"])

    return items


def refresh_projects(enrich_backend):
    logger.debug("Refreshing project field in %s",
                 enrich_backend.elastic.anonymize_url(enrich_backend.elastic.index_url))
    total = 0

    eitems = enrich_backend.fetch()
    for eitem in eitems:
        new_project = enrich_backend.get_item_project(eitem)
        eitem.update(new_project)
        yield eitem
        total += 1

    logger.info("Total eitems refreshed for project field %i", total)


def refresh_identities(enrich_backend, author_field=None, author_values=None):
    """Refresh identities in enriched index.

    Retrieve items from the enriched index corresponding to enrich_backend,
    and update their identities information, with fresh data from the
    SortingHat database.

    Instead of the whole index, only items matching the filter_author
    filter are fitered, if that parameters is not None.

    :param enrich_backend: enriched backend to update
    :param  author_field: field to match items authored by a user
    :param  author_values: values of the authored field to match items
    """

    def update_items(new_filter_author):

        for eitem in enrich_backend.fetch(new_filter_author):
            roles = None
            try:
                roles = enrich_backend.roles
            except AttributeError:
                pass
            new_identities = enrich_backend.get_item_sh_from_id(eitem, roles)
            eitem.update(new_identities)
            yield eitem

    logger.debug("Refreshing identities fields from %s",
                 enrich_backend.elastic.anonymize_url(enrich_backend.elastic.index_url))

    total = 0

    max_ids = enrich_backend.elastic.max_items_clause
    logger.debug('Refreshing identities')

    if author_field is None:
        # No filter, update all items
        for item in update_items(None):
            yield item
            total += 1
    else:
        to_refresh = []
        for author_value in author_values:
            to_refresh.append(author_value)

            if len(to_refresh) > max_ids:
                filter_author = {"name": author_field,
                                 "value": to_refresh}

                for item in update_items(filter_author):
                    yield item
                    total += 1

                to_refresh = []

        if len(to_refresh) > 0:
            filter_author = {"name": author_field,
                             "value": to_refresh}

            for item in update_items(filter_author):
                yield item
                total += 1

    logger.info("Total eitems refreshed for identities fields %i", total)


def load_identities(ocean_backend, enrich_backend):
    # First we add all new identities to SH
    items_count = 0
    identities_count = 0
    new_identities = []

    # Support that ocean_backend is a list of items (old API)
    if isinstance(ocean_backend, list):
        items = ocean_backend
    else:
        items = ocean_backend.fetch()

    for item in items:
        items_count += 1
        # Get identities from new items to be added to SortingHat
        identities = enrich_backend.get_identities(item)

        if not identities:
            continue

        for identity in identities:
            if identity not in new_identities:
                new_identities.append(identity)

            if len(new_identities) > 100:
                inserted_identities = load_bulk_identities(items_count,
                                                           new_identities,
                                                           enrich_backend.sh_db,
                                                           enrich_backend.get_connector_name())
                identities_count += inserted_identities
                new_identities = []

    if new_identities:
        inserted_identities = load_bulk_identities(items_count,
                                                   new_identities,
                                                   enrich_backend.sh_db,
                                                   enrich_backend.get_connector_name())
        identities_count += inserted_identities

    return identities_count


def load_bulk_identities(items_count, new_identities, sh_db, connector_name):
    identities_count = len(new_identities)

    SortingHat.add_identities(sh_db, new_identities, connector_name)

    logger.debug("Processed %i items identities (%i identities) from %s",
                 items_count, len(new_identities), connector_name)

    return identities_count


def enrich_items(ocean_backend, enrich_backend, events=False):
    total = 0

    if not events:
        total = enrich_backend.enrich_items(ocean_backend)
        enrich_backend.update_items(ocean_backend, enrich_backend)
    else:
        total = enrich_backend.enrich_events(ocean_backend)
    return total


def get_ocean_backend(backend_cmd, enrich_backend, no_incremental,
                      filter_raw=None, filter_raw_should=None):
    """ Get the ocean backend configured to start from the last enriched date """

    if no_incremental:
        last_enrich = None
    else:
        last_enrich = get_last_enrich(backend_cmd, enrich_backend)

    logger.debug("Last enrichment: %s", last_enrich)

    backend = None

    connector = get_connectors()[enrich_backend.get_connector_name()]

    if backend_cmd:
        backend_cmd = init_backend(backend_cmd)
        backend = backend_cmd.backend

        signature = inspect.signature(backend.fetch)
        if 'from_date' in signature.parameters:
            ocean_backend = connector[1](backend, from_date=last_enrich)
        elif 'offset' in signature.parameters:
            ocean_backend = connector[1](backend, offset=last_enrich)
        else:
            if last_enrich:
                ocean_backend = connector[1](backend, from_date=last_enrich)
            else:
                ocean_backend = connector[1](backend)
    else:
        # We can have params for non perceval backends also
        params = enrich_backend.backend_params
        if params:
            try:
                date_pos = params.index('--from-date')
                last_enrich = parser.parse(params[date_pos + 1])
            except ValueError:
                pass
        if last_enrich:
            ocean_backend = connector[1](backend, from_date=last_enrich)
        else:
            ocean_backend = connector[1](backend)

    if filter_raw:
        ocean_backend.set_filter_raw(filter_raw)
    if filter_raw_should:
        ocean_backend.set_filter_raw_should(filter_raw_should)

    return ocean_backend


def do_studies(ocean_backend, enrich_backend, studies_args, retention_hours=None):
    """Execute studies related to a given enrich backend. If `retention_hours` is not None, the
    study data is deleted based on the number of `retention_hours`.

    :param ocean_backend: backend to access raw items
    :param enrich_backend: backend to access enriched items
    :param retention_hours: maximum number of hours wrt the current date to retain the data
    :param studies_args: list of studies to be executed
    """
    for study in enrich_backend.studies:
        selected_studies = [(s['name'], s['params']) for s in studies_args if s['type'] == study.__name__]

        for (name, params) in selected_studies:
            logger.info("Starting study: %s, params %s", name, str(params))
            try:
                study(ocean_backend, enrich_backend, **params)
            except Exception as e:
                logger.error("Problem executing study %s, %s", name, str(e))
                raise e

            # identify studies which creates other indexes. If the study is onion,
            # it can be ignored since the index is recreated every week
            if name.startswith('enrich_onion'):
                continue

            index_params = [p for p in params if 'out_index' in p]

            for ip in index_params:
                index_name = params[ip]
                elastic = get_elastic(enrich_backend.elastic_url, index_name)

                elastic.delete_items(retention_hours)


def enrich_backend(url, clean, backend_name, backend_params, cfg_section_name,
                   ocean_index=None,
                   ocean_index_enrich=None,
                   db_projects_map=None, json_projects_map=None,
                   db_sortinghat=None,
                   no_incremental=False, only_identities=False,
                   github_token=None, studies=False, only_studies=False,
                   url_enrich=None, events_enrich=False,
                   db_user=None, db_password=None, db_host=None,
                   do_refresh_projects=False, do_refresh_identities=False,
                   author_id=None, author_uuid=None, filter_raw=None,
                   filters_raw_prefix=None, jenkins_rename_file=None,
                   unaffiliated_group=None, pair_programming=False,
                   node_regex=False, studies_args=None, es_enrich_aliases=None,
                   last_enrich_date=None):
    """ Enrich Ocean index """

    backend = None
    enrich_index = None

    if ocean_index or ocean_index_enrich:
        clean = False  # don't remove index, it could be shared

    if do_refresh_projects or do_refresh_identities:
        clean = False  # refresh works over the existing enriched items

    if not get_connector_from_name(backend_name):
        raise RuntimeError("Unknown backend %s" % backend_name)
    connector = get_connector_from_name(backend_name)
    klass = connector[3]  # BackendCmd for the connector

    try:
        backend = None
        backend_cmd = None
        if klass:
            # Data is retrieved from Perceval
            backend_cmd = init_backend(klass(*backend_params))
            backend = backend_cmd.backend

        if ocean_index_enrich:
            enrich_index = ocean_index_enrich
        else:
            if not ocean_index:
                ocean_index = backend_name + "_" + backend.origin
            enrich_index = ocean_index + "_enrich"
        if events_enrich:
            enrich_index += "_events"

        enrich_backend = connector[2](db_sortinghat, db_projects_map, json_projects_map,
                                      db_user, db_password, db_host)
        enrich_backend.set_params(backend_params)
        # store the cfg section name in the enrich backend to recover the corresponding project name in projects.json
        enrich_backend.set_cfg_section_name(cfg_section_name)
        enrich_backend.set_from_date(last_enrich_date)
        if url_enrich:
            elastic_enrich = get_elastic(url_enrich, enrich_index, clean, enrich_backend, es_enrich_aliases)
        else:
            elastic_enrich = get_elastic(url, enrich_index, clean, enrich_backend, es_enrich_aliases)
        enrich_backend.set_elastic(elastic_enrich)
        if github_token and backend_name == "git":
            enrich_backend.set_github_token(github_token)
        if jenkins_rename_file and backend_name == "jenkins":
            enrich_backend.set_jenkins_rename_file(jenkins_rename_file)
        if unaffiliated_group:
            enrich_backend.unaffiliated_group = unaffiliated_group
        if pair_programming:
            enrich_backend.pair_programming = pair_programming
        if node_regex:
            enrich_backend.node_regex = node_regex

        # The filter raw is needed to be able to assign the project value to an enriched item
        # see line 544, grimoire_elk/enriched/enrich.py (fltr = eitem['origin'] + ' --filter-raw=' + self.filter_raw)
        if filter_raw:
            enrich_backend.set_filter_raw(filter_raw)
        elif filters_raw_prefix:
            enrich_backend.set_filter_raw_should(filters_raw_prefix)

        ocean_backend = get_ocean_backend(backend_cmd, enrich_backend,
                                          no_incremental, filter_raw,
                                          filters_raw_prefix)

        if only_studies:
            logger.info("Running only studies (no SH and no enrichment)")
            do_studies(ocean_backend, enrich_backend, studies_args)
        elif do_refresh_projects:
            logger.info("Refreshing project field in %s",
                        enrich_backend.elastic.anonymize_url(enrich_backend.elastic.index_url))
            field_id = enrich_backend.get_field_unique_id()
            eitems = refresh_projects(enrich_backend)
            enrich_backend.elastic.bulk_upload(eitems, field_id)
        elif do_refresh_identities:

            author_attr = None
            author_values = None
            if author_id:
                author_attr = 'author_id'
                author_values = [author_id]
            elif author_uuid:
                author_attr = 'author_uuid'
                author_values = [author_uuid]

            logger.info("Refreshing identities fields in %s",
                        enrich_backend.elastic.anonymize_url(enrich_backend.elastic.index_url))

            field_id = enrich_backend.get_field_unique_id()
            eitems = refresh_identities(enrich_backend, author_attr, author_values)
            enrich_backend.elastic.bulk_upload(eitems, field_id)
        else:
            clean = False  # Don't remove ocean index when enrich
            elastic_ocean = get_elastic(url, ocean_index, clean, ocean_backend)
            ocean_backend.set_elastic(elastic_ocean)

            logger.info("Adding enrichment data to %s",
                        enrich_backend.elastic.anonymize_url(enrich_backend.elastic.index_url))

            if db_sortinghat and enrich_backend.has_identities():
                # FIXME: This step won't be done from enrich in the future
                total_ids = load_identities(ocean_backend, enrich_backend)
                logger.info("Total identities loaded %i ", total_ids)

            if only_identities:
                logger.info("Only SH identities added. Enrich not done!")

            else:
                # Enrichment for the new items once SH update is finished
                if not events_enrich:
                    enrich_count = enrich_items(ocean_backend, enrich_backend)
                    if enrich_count is not None:
                        logger.info("Total items enriched %i ", enrich_count)
                else:
                    enrich_count = enrich_items(ocean_backend, enrich_backend, events=True)
                    if enrich_count is not None:
                        logger.info("Total events enriched %i ", enrich_count)
                if studies:
                    do_studies(ocean_backend, enrich_backend, studies_args)

    except Exception as ex:
        if backend:
            logger.error("Error enriching ocean from %s (%s): %s",
                         backend_name, backend.origin, ex, exc_info=True)
        else:
            logger.error("Error enriching ocean %s", ex, exc_info=True)

    logger.info("Done %s ", backend_name)


def init_backend(backend_cmd):
    """Init backend within the backend_cmd"""

    try:
        backend_cmd.backend
    except AttributeError:
        parsed_args = vars(backend_cmd.parsed_args)
        init_args = find_signature_parameters(backend_cmd.BACKEND,
                                              parsed_args)
        backend_cmd.backend = backend_cmd.BACKEND(**init_args)

    return backend_cmd


def populate_identities_index(es_enrichment_url, enrich_index):
    """Save the identities currently in use in the index .identities.

    :param es_enrichment_url: url of the ElasticSearch with enriched data
    :param enrich_index: name of the enriched index
    """
    class Mapping(BaseMapping):

        @staticmethod
        def get_elastic_mappings(es_major):
            """Get Elasticsearch mapping.

            :param es_major: major version of Elasticsearch, as string
            :returns:        dictionary with a key, 'items', with the mapping
            """

            mapping = """
            {
                "properties": {
                   "sh_uuid": {
                       "type": "keyword"
                   },
                   "last_seen": {
                       "type": "date"
                   }
                }
            }
            """

            return {"items": mapping}

    # identities index
    mapping_identities_index = Mapping()
    elastic_identities = get_elastic(es_enrichment_url, IDENTITIES_INDEX, mapping=mapping_identities_index)

    # enriched index
    elastic_enrich = get_elastic(es_enrichment_url, enrich_index)
    # collect mapping attributes in enriched index
    attributes = elastic_enrich.all_properties()
    # select attributes coming from SortingHat (*_uuid except git_uuid)
    sh_uuid_attributes = [attr for attr in attributes if attr.endswith('_uuid') and not attr.startswith('git_')]

    enriched_items = ElasticItems(None)
    enriched_items.elastic = elastic_enrich

    logger.debug("Add identities to %s", IDENTITIES_INDEX)

    identities = []
    for eitem in enriched_items.fetch(ignore_incremental=True):
        for sh_uuid_attr in sh_uuid_attributes:

            identity = {
                'sh_uuid': eitem[sh_uuid_attr],
                'last_seen': datetime_utcnow().isoformat()
            }

            identities.append(identity)

            if len(identities) == elastic_enrich.max_items_bulk:
                elastic_identities.bulk_upload(identities, 'sh_uuid')
                identities = []

    if len(identities) > 0:
        elastic_identities.bulk_upload(identities, 'sh_uuid')

    logger.debug("Add identities to %s", IDENTITIES_INDEX)
