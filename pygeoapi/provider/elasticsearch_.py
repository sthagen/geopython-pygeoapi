# =================================================================
#
# Authors: Tom Kralidis <tomkralidis@gmail.com>
#          Francesco Bartoli <xbartolone@gmail.com>
#
# Copyright (c) 2025 Tom Kralidis
# Copyright (c) 2025 Francesco Bartoli
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation
# files (the "Software"), to deal in the Software without
# restriction, including without limitation the rights to use,
# copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the
# Software is furnished to do so, subject to the following
# conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES
# OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
# HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
# WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
# OTHER DEALINGS IN THE SOFTWARE.
#
# =================================================================

from typing import Dict
from collections import OrderedDict
import json
import logging
import uuid

from elasticsearch import Elasticsearch, exceptions, helpers
from pygeofilter.backends.elasticsearch import to_filter

from elasticsearch_dsl import Search

from pygeoapi.provider.base import (BaseProvider, ProviderConnectionError,
                                    ProviderQueryError,
                                    ProviderItemNotFoundError)
from pygeoapi.util import crs_transform


LOGGER = logging.getLogger(__name__)


class ElasticsearchProvider(BaseProvider):
    """Elasticsearch Provider"""

    def __init__(self, provider_def):
        """
        Initialize object

        :param provider_def: provider definition

        :returns: pygeoapi.provider.elasticsearch_.ElasticsearchProvider
        """

        super().__init__(provider_def)

        self.select_properties = []

        self.es_host, self.index_name = self.data.rsplit('/', 1)

        LOGGER.debug('Setting Elasticsearch properties')

        LOGGER.debug(f'host: {self.es_host}')
        LOGGER.debug(f'index: {self.index_name}')

        LOGGER.debug('Connecting to Elasticsearch')
        self.es = Elasticsearch(self.es_host)
        if not self.es.ping():
            msg = f'Cannot connect to Elasticsearch: {self.es_host}'
            LOGGER.error(msg)
            raise ProviderConnectionError(msg)

        LOGGER.debug('Determining ES version')
        v = self.es.info()['version']['number'][:3]
        if float(v) < 8:
            msg = 'only ES 8+ supported'
            LOGGER.error(msg)
            raise ProviderConnectionError(msg)

        LOGGER.debug('Grabbing field information')
        try:
            self.get_fields()
        except exceptions.NotFoundError as err:
            LOGGER.error(err)
            raise ProviderQueryError(err)

    def get_fields(self):
        """
         Get provider field information (names, types)

        :returns: dict of fields
        """
        if not self._fields:
            ii = self.es.indices.get(index=self.index_name,
                                     allow_no_indices=False)

            LOGGER.debug(f'Response: {ii}')
            try:
                if '*' not in self.index_name:
                    mappings = ii[self.index_name]['mappings']
                    p = mappings['properties']['properties']
                else:
                    LOGGER.debug('Wildcard index; setting from first match')
                    index_name_ = list(ii.keys())[0]
                    p = ii[index_name_]['mappings']['properties']['properties']
            except KeyError:
                LOGGER.warning('Trying for alias')
                alias_name = next(iter(ii))
                p = ii[alias_name]['mappings']['properties']['properties']
            except IndexError:
                LOGGER.warning('could not get fields; returning empty set')
                return {}

            self._fields = self.get_nested_fields(p, self._fields)
        return self._fields

    def get_nested_fields(self, properties, fields, prev_field=None):
        """
        Get Elasticsearch fields (names, types) for all nested properties

        :param properties: `dict` of Elasticsearch mappings properties
        :param fields: `dict` of fields in the current iteration
        :param prev_field: name of the parent field

        :returns: dict of fields
        """
        for k, v in properties['properties'].items():

            cur_field = k if prev_field is None else f'{prev_field}.{k}'

            if isinstance(v, dict) and 'properties' in v:
                fields = self.get_nested_fields(v, fields, cur_field)
            else:
                if v['type'] == 'text':
                    fields[cur_field] = {'type': 'string'}
                elif v['type'] == 'date':
                    fields[cur_field] = {'type': 'string', 'format': 'date'}
                elif v['type'] in ('float', 'long'):
                    fields[cur_field] = {'type': 'number', 'format': v['type']}
                else:
                    fields[cur_field] = {'type': v['type']}
        return fields

    def get_domains(self, properties=[], current=False) -> tuple:
        """
        Get domains from dataset

        :param properties: `list` of property names
        :param current: `bool` of whether to provide list of live
                        values (default `False`)

        :returns: `tuple` of domains and whether they are based on the
                  current/live dataset
        """

        domains = {}

        if properties:
            keys = properties
        else:
            keys = self.fields.keys()

        body = {
            'size': 0,
            'aggs': {}
        }

        for key in keys:
            if (self.fields[key]['type'] == 'string'
                    and self.fields[key].get('format') != 'date'):

                LOGGER.debug('setting ES .raw on property')
                agg_property = f'{self.mask_prop(key)}.raw'
            else:
                agg_property = f'{self.mask_prop(key)}'

            body['aggs'][key] = {
                'terms': {
                    'field': agg_property,
                    'size': 500
                }
            }

        response = self.es.search(index=self.index_name, body=body)

        for key, value in response['aggregations'].items():
            if self.fields[key]['type'] in ['string', 'number']:
                values = [x['key'] for x in value['buckets']]
                values = [x for x in values if isinstance(x, (float, int, str))]  # noqa
                if values:
                    domains[key] = values

        return domains, True

    @crs_transform
    def query(self, offset=0, limit=10, resulttype='results',
              bbox=[], datetime_=None, properties=[], sortby=[],
              select_properties=[], skip_geometry=False, q=None,
              filterq=None, **kwargs):
        """
        query Elasticsearch index

        :param offset: starting record to return (default 0)
        :param limit: number of records to return (default 10)
        :param resulttype: return results or hit limit (default results)
        :param bbox: bounding box [minx,miny,maxx,maxy]
        :param datetime_: temporal (datestamp or extent)
        :param properties: list of tuples (name, value)
        :param sortby: list of dicts (property, order)
        :param select_properties: list of property names
        :param skip_geometry: bool of whether to skip geometry (default False)
        :param q: full-text search term(s)
        :param filterq: filter object

        :returns: dict of 0..n GeoJSON features
        """

        self.select_properties = select_properties

        query = {'track_total_hits': True, 'query': {'bool': {'filter': []}}}
        filter_ = []

        feature_collection = {
            'type': 'FeatureCollection',
            'features': []
        }

        if resulttype == 'hits':
            LOGGER.debug('hits only specified')
            limit = 0

        if bbox:
            LOGGER.debug('processing bbox parameter')
            minx, miny, maxx, maxy = bbox
            bbox_filter = {
                'geo_shape': {
                    'geometry': {
                        'shape': {
                            'type': 'envelope',
                            'coordinates': [[minx, maxy], [maxx, miny]]
                        },
                        'relation': 'intersects'
                    }
                }
            }

            query['query']['bool']['filter'].append(bbox_filter)

        if datetime_ is not None:
            LOGGER.debug('processing datetime parameter')
            if self.time_field is None:
                LOGGER.error('time_field not enabled for collection')
                raise ProviderQueryError()

            time_field = self.mask_prop(self.time_field)

            if '/' in datetime_:  # envelope
                LOGGER.debug('detected time range')
                time_begin, time_end = datetime_.split('/')

                range_ = {
                    'range': {
                        time_field: {
                            'gte': time_begin,
                            'lte': time_end
                        }
                    }
                }
                if time_begin == '..':
                    range_['range'][time_field].pop('gte')
                elif time_end == '..':
                    range_['range'][time_field].pop('lte')

                filter_.append(range_)

            else:  # time instant
                LOGGER.debug('detected time instant')
                filter_.append({'match': {time_field: datetime_}})

            LOGGER.debug(filter_)
            query['query']['bool']['filter'].append(*filter_)

        if properties:
            LOGGER.debug('processing properties')
            for prop in properties:
                prop_name = self.mask_prop(prop[0])
                pf = {
                    'match': {
                        prop_name: {
                            'query': prop[1]
                        }
                    }
                }
                query['query']['bool']['filter'].append(pf)

            if '|' not in prop[1]:
                pf['match'][prop_name]['minimum_should_match'] = '100%'

        if sortby:
            LOGGER.debug('processing sortby')
            query['sort'] = []
            for sort in sortby:
                LOGGER.debug(f'processing sort object: {sort}')

                sp = sort['property']

                if (self.fields[sp]['type'] == 'string'
                        and self.fields[sp].get('format') != 'date'):
                    LOGGER.debug('setting ES .raw on property')
                    sort_property = f'{self.mask_prop(sp)}.raw'
                else:
                    sort_property = self.mask_prop(sp)

                sort_order = 'asc'
                if sort['order'] == '-':
                    sort_order = 'desc'

                sort_ = {
                    sort_property: {
                        'order': sort_order
                    }
                }
                query['sort'].append(sort_)

        if q is not None:
            LOGGER.debug('Adding free-text search')
            # split inclusive on ',' (OR)
            q_tokens = [f'"{t}"' for t in q.split(',')]
            # enclose each token as a search phrase
            q2 = ' OR '.join(q_tokens)
            query['query']['bool']['must'] = {'query_string': {'query': q2}}

            query['_source'] = {
                'excludes': [
                    'properties._metadata-payload',
                    'properties._metadata-schema',
                    'properties._metadata-format'
                ]
            }

        if self.properties or self.select_properties:
            LOGGER.debug('filtering properties')

            all_properties = self.get_properties()

            query['_source'] = {
                'includes': list(map(self.mask_prop, all_properties))
            }

            query['_source']['includes'].append('id')
            query['_source']['includes'].append('type')
            query['_source']['includes'].append('geometry')

        if skip_geometry:
            LOGGER.debug('excluding geometry')
            try:
                query['_source']['excludes'] = ['geometry']
            except KeyError:
                query['_source'] = {'excludes': ['geometry']}
        try:
            LOGGER.debug('querying Elasticsearch')
            if filterq:
                LOGGER.debug(f'adding cql object: {filterq}')
                query = update_query(input_query=query, cql=filterq)
            LOGGER.debug(json.dumps(query, indent=4))

            LOGGER.debug('Testing for ES scrolling')
            if offset + limit > 10000:
                gen = helpers.scan(client=self.es, query=query,
                                   preserve_order=True,
                                   index=self.index_name)
                results = {'hits': {'total': limit, 'hits': []}}
                for i in range(offset + limit):
                    try:
                        if i >= offset:
                            results['hits']['hits'].append(next(gen))
                        else:
                            next(gen)
                    except StopIteration:
                        break

                matched = len(results['hits']['hits']) + offset
                returned = len(results['hits']['hits'])
            else:
                es_results = self.es.search(index=self.index_name,
                                            from_=offset, size=limit, **query)
                results = es_results
                matched = es_results['hits']['total']['value']
                returned = len(es_results['hits']['hits'])

        except exceptions.ConnectionError as err:
            LOGGER.error(err)
            raise ProviderConnectionError()
        except exceptions.RequestError as err:
            LOGGER.error(err)
            raise ProviderQueryError()
        except exceptions.NotFoundError as err:
            LOGGER.error(err)
            raise ProviderQueryError()

        feature_collection['numberMatched'] = matched

        if resulttype == 'hits':
            return feature_collection

        feature_collection['numberReturned'] = returned

        LOGGER.debug('serializing features')
        for feature in results['hits']['hits']:
            feature_ = self.esdoc2geojson(feature)
            feature_collection['features'].append(feature_)

        return feature_collection

    @crs_transform
    def get(self, identifier, **kwargs):
        """
        Get ES document by id

        :param identifier: feature id

        :returns: dict of single GeoJSON feature
        """

        try:
            LOGGER.debug(f'Fetching identifier {identifier}')
            result = self.es.get(index=self.index_name, id=identifier)
            LOGGER.debug('Serializing feature')
            feature_ = self.esdoc2geojson(result)
        except Exception as err:
            LOGGER.debug(f'Not found via ES id query: {err}')
            LOGGER.debug('Trying via a real query')

            query = {
                'query': {
                    'bool': {
                        'filter': [{
                            'match_phrase': {
                                '_id': identifier
                            }
                        }]
                    }
                }
            }

            LOGGER.debug(f'Query: {query}')
            try:
                result = self.es.search(index=self.index_name, **query)
                if len(result['hits']['hits']) == 0:
                    LOGGER.error(err)
                    raise ProviderItemNotFoundError(err)
                LOGGER.debug('Serializing feature')
                feature_ = self.esdoc2geojson(result['hits']['hits'][0])
            except exceptions.RequestError as err2:
                LOGGER.error(err2)
                raise ProviderItemNotFoundError(err2)
        except Exception as err:
            LOGGER.error(err)
            return None

        return feature_

    def create(self, item):
        """
        Create a new item

        :param item: `dict` of new item

        :returns: identifier of created item
        """

        identifier, json_data = self._load_and_prepare_item(
            item, accept_missing_identifier=True)
        if identifier is None:
            # If there is no incoming identifier, allocate a random one
            identifier = str(uuid.uuid4())
            json_data["id"] = identifier

        LOGGER.debug(f'Inserting data with identifier {identifier}')
        _ = self.es.index(index=self.index_name, id=identifier, body=json_data)
        LOGGER.debug('Item added')

        return identifier

    def update(self, identifier, item):
        """
        Updates an existing item

        :param identifier: feature id
        :param item: `dict` of partial or full item

        :returns: `bool` of update result
        """

        LOGGER.debug(f'Updating item {identifier}')
        identifier, json_data = self._load_and_prepare_item(
            item, identifier, raise_if_exists=False)

        _ = self.es.index(index=self.index_name, id=identifier, body=json_data)

        return True

    def delete(self, identifier):
        """
        Deletes an existing item

        :param identifier: item id

        :returns: `bool` of deletion result
        """

        LOGGER.debug(f'Deleting item {identifier}')
        _ = self.es.delete(index=self.index_name, id=identifier)

        return True

    def esdoc2geojson(self, doc):
        """
        generate GeoJSON `dict` from ES document

        :param doc: `dict` of ES document

        :returns: GeoJSON `dict`
        """

        feature_ = {}
        feature_thinned = {}

        LOGGER.debug('Fetching id and geometry from GeoJSON document')
        feature_ = doc['_source']

        try:
            id_ = doc['_source']['properties'][self.id_field]
        except KeyError as err:
            LOGGER.debug(f'Missing field: {err}')
            id_ = doc['_source'].get('id', doc['_id'])

        feature_['id'] = id_
        feature_['geometry'] = doc['_source'].get('geometry')

        if self.properties or self.select_properties:
            LOGGER.debug('Filtering properties')
            all_properties = self.get_properties()

            feature_thinned = {
                'id': id_,
                'type': feature_['type'],
                'geometry': feature_.get('geometry'),
                'properties': OrderedDict()
            }
            for p in all_properties:
                try:
                    feature_thinned['properties'][p] = feature_['properties'][p]  # noqa
                except KeyError as err:
                    msg = f'Property missing {err}; continuing'
                    LOGGER.warning(msg)

        if feature_thinned:
            return feature_thinned
        else:
            return feature_

    def mask_prop(self, property_name):
        """
        generate property name based on ES backend setup

        :param property_name: property name

        :returns: masked property name
        """

        return f'properties.{property_name}'

    def get_properties(self):
        all_properties = []

        LOGGER.debug(f'configured properties: {self.properties}')
        LOGGER.debug(f'selected properties: {self.select_properties}')

        if not self.properties and not self.select_properties:
            all_properties = self.get_fields()
        if self.properties and self.select_properties:
            all_properties = self.properties and self.select_properties
        else:
            all_properties = self.properties or self.select_properties

        LOGGER.debug(f'resulting properties: {all_properties}')
        return all_properties

    def __repr__(self):
        return f'<ElasticsearchProvider> {self.data}'


class ElasticsearchCatalogueProvider(ElasticsearchProvider):
    """Elasticsearch Provider"""

    def __init__(self, provider_def):
        super().__init__(provider_def)

    def _excludes(self):
        return [
            'properties._metadata-anytext'
        ]

    def get_fields(self):
        fields = super().get_fields()
        for i in self._excludes():
            if i in fields:
                del fields[i]

        fields['q'] = {'type': 'string'}

        return fields

    def query(self, offset=0, limit=10, resulttype='results',
              bbox=[], datetime_=None, properties=[], sortby=[],
              select_properties=[], skip_geometry=False, q=None,
              filterq=None, **kwargs):

        records = super().query(
            offset=offset, limit=limit,
            resulttype=resulttype, bbox=bbox,
            datetime_=datetime_, properties=properties,
            sortby=sortby,
            select_properties=select_properties,
            skip_geometry=skip_geometry,
            q=q)

        return records

    def __repr__(self):
        return f'<ElasticsearchCatalogueProvider> {self.data}'


def update_query(input_query: Dict, cql):
    s = Search.from_dict(input_query)
    s = s.query(to_filter(cql))
    LOGGER.debug(f'Enhanced query: {json.dumps(s.to_dict())}')
    return s.to_dict()
