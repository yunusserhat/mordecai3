from elasticsearch import Elasticsearch, helpers
from elasticsearch_dsl import Search, Q
import numpy as np
import jellyfish
from collections import Counter
import warnings

def make_conn():
    kwargs = dict(
        hosts=['localhost'],
        port=9200,
        use_ssl=False,
    )
    CLIENT = Elasticsearch(**kwargs)
    conn = Search(using=CLIENT, index="geonames")
    return conn

def normalize(ll: list) -> np.array:    
    """Normalize an array to [0, 1]"""
    ll = np.array(ll)
    if len(ll) > 0:
        max_ll = np.max(ll)
        if max_ll == 0:
            max_ll = 0.001
        ll = (ll - np.min(ll)) / max_ll
    return ll


def make_admin1_counts(out):
    """Take in a document's worth of examples and return the count of adm1s"""
    admin1s = []
    
    for n, es in enumerate(out):
        other_adm1 = set([i['admin1_code'] for i in es['es_choices']])
        admin1s.extend(list(other_adm1))
    
    admin1_count = dict(Counter(admin1s))
    for k, v in admin1_count.items():
        admin1_count[k] = v / len(out)
    return admin1_count

def make_country_counts(out):
    """Take in a document's worth of examples and return the count of countries"""
    all_countries = []
    for es in out:
        countries = set([i['country_code3'] for i in es['es_choices']])
        all_countries.extend(list(countries))
    
    country_count = dict(Counter(all_countries))
    for k, v in country_count.items():
        country_count[k] = v / len(out)
        
    return country_count

def res_formatter(res, placename):
    """
    Helper function to format the ES/Geonames results into a format for the ML model, including
    edit distance statistics.

    Parameters
    ----------
    res: Elasticsearch/Geonames output
    placename: str
      The original search term

    Returns
    -------
    choices: list
      List of formatted Geonames results, including edit distance statistics
    """
    choices = []
    alt_lengths = []
    min_dist = []
    max_dist = []
    avg_dist = []
    for i in res['hits']['hits']:
        i = i.to_dict()['_source']
        names = [i['name']] + i['alternativenames'] 
        dists = [jellyfish.levenshtein_distance(placename, j) for j in names]
        lat, lon = i['coordinates'].split(",")
        d = {"feature_code": i['feature_code'],
            "feature_class": i['feature_class'],
            "country_code3": i['country_code3'],
            "lat": float(lat),
            "lon": float(lon),
            "name": i['name'],
            "admin1_code": i['admin1_code'],
            "geonameid": i['geonameid']}
        choices.append(d)
        alt_lengths.append(len(i['alternativenames']))
        dists = [jellyfish.levenshtein_distance(placename, j) for j in names]
        mn = np.min(dists)
        if np.isnan(mn):
            print("min problem")
            mn = 10
            print(dists)
        min_dist.append(mn)
        mx = np.max(dists)
        if np.isnan(mx):
            print("max problem")
            mx = 10
        max_dist.append(mx)
        ag = np.mean(dists)
        if np.isnan(ag):
            print("avg problem")
            ag = 10
        avg_dist.append(ag)
    alt_lengths = normalize(alt_lengths)
    min_dist = normalize(min_dist)
    max_dist = normalize(max_dist)
    avg_dist = normalize(avg_dist)

    for n, i in enumerate(choices):
        i['alt_name_length'] = alt_lengths[n]
        i['min_dist'] = min_dist[n]
        i['max_dist'] = max_dist[n]
        i['avg_dist'] = avg_dist[n]
    return choices


def add_es_data(ex, conn, fuzzy=True):
    """
    Run an Elasticsearch/geonames query for a single example and add the results

    Parameters
    ---------
    ex: dict
      output of doc_to_ex_expanded
    conn: elasticsearch connection

    Examples
    --------
    d = {"placename": ent.text,
         "tensor": tensor,
         "doc_tensor": doc_tensor,
         "locs_tensor": locs_tensor,
         "sent": ent.sent.text,
         "start_char": ent[0].idx,
         "end_char": ent[-1].idx + len(ent.text)}
    """
    q = {"multi_match": {"query": ex['placename'],
                                 "fields": ['name', 'asciiname', 'alternativenames'],
                                "type" : "phrase"}}
    res = conn.query(q).sort({"alt_name_length": {'order': "desc"}})[0:50].execute()
    choices = res_formatter(res, ex['placename'])
    if fuzzy and not choices:
        q = {"multi_match": {"query": ex['placename'],
                             "fields": ['name', 'alternativenames', 'asciiname'],
                             "fuzziness" : 1,
                            }}
        res = conn.query(q)[0:10].execute()
        choices = res_formatter(res, ex['placename'])

    ex['es_choices'] = choices
    if 'correct_geonamesid' in ex.keys():
        ex['correct'] = [c['geonameid'] == ex['correct_geonamesid'] for c in choices]
    return ex

def add_es_data_doc(doc_ex, conn):
    doc_es = []
    for ex in doc_ex:
        with warnings.catch_warnings():
            try:
                es = add_es_data(ex, conn)
                doc_es.append(es)
            except Warning:
                continue
    if not doc_es:
        return []
    admin1_count = make_admin1_counts(doc_es)
    country_count = make_country_counts(doc_es)

    for i in doc_es:
        for e in i['es_choices']:
            e['adm1_count'] = admin1_count[e['admin1_code']]
            e['country_count'] = country_count[e['country_code3']]
    return doc_es


