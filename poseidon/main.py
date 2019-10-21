#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
The main entrypoint for Poseidon, schedules the threads, connects to SDN
controllers and defines the Monitor class.

Created on 3 December 2018
@author: Charlie Lewis
"""
import ast
import difflib
import ipaddress
import json
import logging
import queue
import random
import signal
import sys
import threading
import time
from copy import deepcopy
from functools import partial

import pika
import requests
import schedule
from redis import StrictRedis

from poseidon.constants import NO_DATA
from poseidon.controllers.bcf.bcf import BcfProxy
from poseidon.controllers.faucet.faucet import FaucetProxy
from poseidon.controllers.faucet.parser import Parser
from poseidon.helpers.actions import Actions
from poseidon.helpers.config import Config
from poseidon.helpers.endpoint import Endpoint, EndpointDecoder, endpoint_factory
from poseidon.helpers.endpoint import MACHINE_IP_FIELDS, MACHINE_IP_PREFIXES
from poseidon.helpers.log import Logger
from poseidon.helpers.metadata import get_ether_vendor
from poseidon.helpers.metadata import get_rdns_lookup
from poseidon.helpers.prometheus import Prometheus
from poseidon.helpers.rabbit import Rabbit

requests.packages.urllib3.disable_warnings()
logging.getLogger('pika').setLevel(logging.WARNING)

CTRL_C = dict()
CTRL_C['STOP'] = False
Logger()
logger = logging.getLogger('main')


def rabbit_callback(ch, method, properties, body, q=None):
    ''' callback, places rabbit data into internal queue'''
    logger.debug('got a message: {0}:{1}:{2}'.format(
        method.routing_key, body, type(body)))
    if q is not None:
        q.put((method.routing_key, body))
    else:
        logger.debug('poseidonMain workQueue is None')


def schedule_job_kickurl(schedule_func):
    global CTRL_C
    schedule_func.s.check_endpoints(messages=schedule_func.faucet_event)
    del schedule_func.faucet_event[:]

    if not CTRL_C['STOP']:
        try:
            # get current state
            req = requests.get(
                'http://poseidon-api:8000/v1/network_full', timeout=10)

            # send results to prometheus
            hosts = req.json()['dataset']
            schedule_func.prom.update_metrics(hosts)
        except requests.exceptions.ConnectionError as e:
            schedule_func.logger.debug(
                'Unable to get current state and send it to Prometheus because: {0}'.format(str(e)))
        except Exception as e:  # pragma: no cover
            schedule_func.logger.error(
                'Unable to get current state and send it to Prometheus because: {0}'.format(str(e)))


def schedule_job_reinvestigation(schedule_func):
    ''' put endpoints into the reinvestigation state if possible '''
    global CTRL_C

    def trigger_reinvestigation(candidates):
        # get random order of things that are known
        for _ in range(schedule_func.controller['max_concurrent_reinvestigations'] - schedule_func.s.investigations):
            if len(candidates) > 0:
                chosen = candidates.pop()
                schedule_func.logger.info('Starting reinvestigation on: {0} {1}'.format(
                    chosen.name, chosen.state))
                chosen.reinvestigate()
                chosen.p_prev_states.append(
                    (chosen.state, int(time.time())))
                status = Actions(chosen, schedule_func.s.sdnc).mirror_endpoint()
                if status:
                    try:
                        schedule_func.s.r.hincrby('vent_plugin_counts', 'ncapture')
                    except Exception as e:  # pragma: no cover
                        schedule_func.logger.error(
                            'Failed to update count of plugins because: {0}'.format(str(e)))
                else:
                    schedule_func.logger.warning(
                        'Unable to mirror the endpoint: {0}'.format(chosen.name))
        return

    if not CTRL_C['STOP']:
        candidates = [
            endpoint for endpoint in schedule_func.s.endpoints.values()
            if endpoint.state in ['queued']]
        if len(candidates) == 0:
            # if no queued endpoints, then known and abnormal are candidates
            candidates = [
                endpoint for endpoint in schedule_func.s.endpoints.values()
                if endpoint.state in ['known', 'abnormal']]
            if len(candidates) > 0:
                random.shuffle(candidates)
        if schedule_func.s.sdnc:
            trigger_reinvestigation(candidates)


def schedule_thread_worker(schedule):
    ''' schedule thread, takes care of running processes in the future '''
    global CTRL_C
    logger.debug('Starting thread_worker')
    while not CTRL_C['STOP']:
        sys.stdout.flush()
        schedule.run_pending()
        time.sleep(1)
    logger.debug('Threading stop:{0}'.format(
        threading.current_thread().getName()))
    sys.exit()


class SDNConnect(object):

    def __init__(self, controller):
        self.controller = controller
        self.r = None
        self.first_time = True
        self.sdnc = None
        trunk_ports = self.controller['trunk_ports']
        if isinstance(trunk_ports, str):
            self.trunk_ports = json.loads(trunk_ports)
        else:
            self.trunk_ports = trunk_ports
        self.logger = logger
        self.get_sdn_context()
        self.endpoints = {}
        self.investigations = 0
        self.clear_filters()
        self.redis_lock = threading.Lock()
        self.connect_redis()
        self.default_endpoints()

    def clear_filters(self):
        ''' clear any exisiting filters. '''
        if isinstance(self.sdnc, FaucetProxy):
            Parser().clear_mirrors(self.controller['CONFIG_FILE'])
        elif isinstance(self.sdnc, BcfProxy):
            self.logger.debug('removing bcf filter rules')
            retval = self.sdnc.remove_filter_rules()
            self.logger.debug('removed filter rules: {0}'.format(retval))

    def default_endpoints(self):
        ''' set endpoints to default state. '''
        self.get_stored_endpoints()
        for endpoint in self.endpoints.values():
            if not endpoint.ignore:
                if endpoint.state != 'inactive':
                    if endpoint.state == 'mirroring':
                        endpoint.p_next_state = 'mirror'
                    elif endpoint.state == 'reinvestigating':
                        endpoint.p_next_state = 'reinvestigate'
                    elif endpoint.state == 'queued':
                        endpoint.p_next_state = 'queue'
                    elif endpoint.state in ['known', 'abnormal']:
                        endpoint.p_next_state = endpoint.state
                    endpoint.endpoint_data['active'] = 0
                    endpoint.inactive()
                    endpoint.p_prev_states.append(
                        (endpoint.state, int(time.time())))
        self.store_endpoints()

    def get_stored_endpoints(self):
        ''' load existing endpoints from Redis. '''
        with self.redis_lock:
            endpoints = {}
            if self.r:
                try:
                    p_endpoints = self.r.get('p_endpoints')
                    if p_endpoints:
                        p_endpoints = ast.literal_eval(p_endpoints.decode('ascii'))
                        for p_endpoint in p_endpoints:
                            endpoint = EndpointDecoder(p_endpoint).get_endpoint()
                            endpoints[endpoint.name] = endpoint
                except Exception as e:  # pragma: no cover
                    self.logger.error(
                        'Unable to get existing endpoints from Redis because {0}'.format(str(e)))
            self.endpoints = endpoints
        return

    def get_stored_metadata(self, hash_id):
        mac_addresses = {}
        ip_addresses = {ip_field: {} for ip_field in MACHINE_IP_FIELDS}

        if self.r:
            macs = []
            try:
                macs = self.r.smembers('mac_addresses')
            except Exception as e:  # pragma: no cover
                self.logger.error(
                    'Unable to get existing mac addresses from Redis because: {0}'.format(str(e)))
            for mac in macs:
                try:
                    mac_info = self.r.hgetall(mac)
                    if b'poseidon_hash' in mac_info and mac_info[b'poseidon_hash'] == hash_id.encode('utf-8'):
                        mac_addresses[mac.decode('ascii')] = {}
                        if b'timestamps' in mac_info:
                            try:
                                timestamps = ast.literal_eval(
                                    mac_info[b'timestamps'].decode('ascii'))
                                for timestamp in timestamps:
                                    ml_info = self.r.hgetall(
                                        mac.decode('ascii')+'_'+str(timestamp))
                                    labels = []
                                    if b'labels' in ml_info:
                                        labels = ast.literal_eval(
                                            ml_info[b'labels'].decode('ascii'))
                                    confidences = []
                                    if b'confidences' in ml_info:
                                        confidences = ast.literal_eval(
                                            ml_info[b'confidences'].decode('ascii'))
                                    behavior = 'None'
                                    tmp = []
                                    if mac_info[b'poseidon_hash'] in ml_info:
                                        tmp = ast.literal_eval(
                                            ml_info[mac_info[b'poseidon_hash']].decode('ascii'))
                                    elif mac_info[b'poseidon_hash'].decode('ascii') in ml_info:
                                        tmp = ast.literal_eval(
                                            ml_info[mac_info[b'poseidon_hash'].decode('ascii')].decode('ascii'))
                                    if 'decisions' in tmp and 'behavior' in tmp['decisions']:
                                        behavior = tmp['decisions']['behavior']
                                    mac_addresses[mac.decode('ascii')][str(timestamp)] = {
                                        'labels': labels, 'confidences': confidences, 'behavior': behavior}
                            except Exception as e:  # pragma: no cover
                                self.logger.error(
                                    'Unable to get existing ML data from Redis because: {0}'.format(str(e)))
                        try:
                            poseidon_info = self.r.hgetall(
                                mac_info[b'poseidon_hash'])
                            if b'endpoint_data' in poseidon_info:
                                endpoint_data = ast.literal_eval(
                                    poseidon_info[b'endpoint_data'].decode('ascii'))
                                for ip_field in MACHINE_IP_FIELDS:
                                    try:
                                        raw_field = endpoint_data.get(
                                            ip_field, None)
                                        machine_ip = ipaddress.ip_address(
                                            raw_field)
                                    except ValueError:
                                        machine_ip = ''
                                    if machine_ip:
                                        try:
                                            ip_info = self.r.hgetall(raw_field)
                                            short_os = ip_info.get(
                                                b'short_os', None)
                                            ip_addresses[ip_field][raw_field] = {
                                            }
                                            if short_os:
                                                ip_addresses[ip_field][raw_field]['os'] = short_os.decode(
                                                    'ascii')
                                        except Exception as e:  # pragma: no cover
                                            self.logger.error(
                                                'Unable to get existing {0} data from Redis because: {1}'.format(ip_field, str(e)))
                        except Exception as e:  # pragma: no cover
                            self.logger.error(
                                'Unable to get existing endpoint data from Redis because: {0}'.format(str(e)))
                except Exception as e:  # pragma: no cover
                    self.logger.error(
                        'Unable to get existing metadata for {0} from Redis because: {1}'.format(mac, str(e)))
        return mac_addresses, ip_addresses['ipv4'], ip_addresses['ipv6']

    def get_sdn_context(self):
        if 'TYPE' in self.controller and self.controller['TYPE'] == 'bcf':
            try:
                self.sdnc = BcfProxy(self.controller)
            except Exception as e:  # pragma: no cover
                self.logger.error(
                    'BcfProxy could not connect to {0} because {1}'.format(
                        self.controller['URI'], e))
        elif 'TYPE' in self.controller and self.controller['TYPE'] == 'faucet':
            try:
                self.sdnc = FaucetProxy(self.controller)
            except Exception as e:  # pragma: no cover
                self.logger.error(
                    'FaucetProxy could not connect to {0} because {1}'.format(
                        self.controller['URI'], e))
        elif 'TYPE' in self.controller and self.controller['TYPE'] == 'None':
            self.sdnc = None
        else:
            if 'CONTROLLER_PASS' in self.controller:
                self.controller['CONTROLLER_PASS'] = '********'
            self.logger.error(
                'Unknown SDN controller config: {0}'.format(
                    self.controller))

    def endpoint_by_name(self, name):
        return self.endpoints.get(name, None)

    def endpoint_by_hash(self, hash_id):
        return self.endpoint_by_name(hash_id)

    def endpoints_by_ip(self, ip):
        endpoints = [
            endpoint for endpoint in self.endpoints.values()
            if ip == endpoint.endpoint_data.get('ipv4', None) or
            ip == endpoint.endpoint_data.get('ipv6', None)]
        return endpoints

    def endpoints_by_mac(self, mac):
        endpoints = [
            endpoint for endpoint in self.endpoints.values()
            if mac == endpoint.endpoint_data['mac']]
        return endpoints

    @staticmethod
    def _connect_rabbit():
        # Rabbit settings
        exchange = 'topic-poseidon-internal'
        exchange_type = 'topic'

        # Starting rabbit connection
        connection = pika.BlockingConnection(
            pika.ConnectionParameters(host='RABBIT_SERVER')
        )

        channel = connection.channel()
        channel.exchange_declare(
            exchange=exchange, exchange_type=exchange_type
        )

        return channel, exchange, connection

    @staticmethod
    def publish_action(action, message):
        try:
            channel, exchange, connection = SDNConnect._connect_rabbit()
            channel.basic_publish(exchange=exchange,
                                  routing_key=action,
                                  body=message)
            connection.close()
        except Exception as e:  # pragma: no cover
            pass
        return

    def show_endpoints(self, arg):
        endpoints = []
        if arg == 'all':
            endpoints = list(self.endpoints.values())
        else:
            show_type, arg = arg.split(' ', 1)
            for endpoint in self.endpoints.values():
                if show_type == 'state':
                    if arg == 'active' and endpoint.state != 'inactive':
                        endpoints.append(endpoint)
                    elif arg == 'ignored' and endpoint.ignore:
                        endpoints.append(endpoint)
                    elif endpoint.state == arg:
                        endpoints.append(endpoint)
                elif show_type in ['os', 'behavior', 'role']:
                    # filter by device type or behavior
                    if 'mac_addresses' in endpoint.metadata and endpoint.endpoint_data['mac'] in endpoint.metadata['mac_addresses']:
                        timestamps = endpoint.metadata['mac_addresses'][endpoint.endpoint_data['mac']]
                        newest = '0'
                        for timestamp in timestamps:
                            if timestamp > newest:
                                newest = timestamp
                        if newest is not '0':
                            if 'labels' in timestamps[newest]:
                                if arg.replace('-', ' ') == timestamps[newest]['labels'][0].lower():
                                    endpoints.append(endpoint)
                            if 'behavior' in timestamps[newest]:
                                if arg == timestamps[newest]['behavior'].lower():
                                    endpoints.append(endpoint)

                    # filter by operating system
                    for ip_field in MACHINE_IP_FIELDS:
                        ip_addresses_field = '_'.join((ip_field, 'addresses'))
                        ip_addresses = endpoint.metadata.get(
                            ip_addresses_field, None)
                        machine_ip = endpoint.endpoint_data.get(ip_field, None)
                        if machine_ip and ip_addresses and machine_ip in ip_addresses:
                            metadata = ip_addresses[machine_ip]
                            os = metadata.get('os', None)
                            if os and os.lower() == arg:
                                endpoints.append(endpoint)
        return endpoints

    def check_endpoints(self, messages=None):
        if not self.sdnc:
            return

        retval = {}
        retval['machines'] = None
        retval['resp'] = 'bad'

        current = None
        parsed = None

        try:
            current = self.sdnc.get_endpoints(messages=messages)
            parsed = self.sdnc.format_endpoints(
                current, self.controller['URI'])
            retval['machines'] = parsed
            retval['resp'] = 'ok'
        except Exception as e:  # pragma: no cover
            self.logger.error(
                'Could not establish connection to {0} because {1}.'.format(
                    self.controller['URI'], e))
            retval['controller'] = 'Could not establish connection to {0}.'.format(
                self.controller['URI'])

        self.find_new_machines(parsed)

        return

    def connect_redis(self, host='redis', port=6379, db=0):
        self.r = None
        try:
            self.r = StrictRedis(host=host, port=port, db=db,
                                 socket_connect_timeout=2)
        except Exception as e:  # pragma: no cover
            self.logger.error(
                'Failed connect to Redis because: {0}'.format(str(e)))
        return

    @staticmethod
    def _diff_machine(machine_a, machine_b):

        def _machine_strlines(machine):
            return str(json.dumps(machine, indent=2)).splitlines()

        machine_a_strlines = _machine_strlines(machine_a)
        machine_b_strlines = _machine_strlines(machine_b)
        return '\n'.join(difflib.unified_diff(
            machine_a_strlines, machine_b_strlines, n=1))

    @staticmethod
    def _parse_machine_ip(machine):
        machine_ip_data = {}
        for ip_field, fields in MACHINE_IP_FIELDS.items():
            try:
                raw_field = machine.get(ip_field, None)
                machine_ip = ipaddress.ip_address(raw_field)
                machine_subnet = ipaddress.ip_network(machine_ip).supernet(
                    new_prefix=MACHINE_IP_PREFIXES[ip_field])
            except ValueError:
                machine_ip = None
                machine_subnet = None
            machine_ip_data[ip_field] = ''
            if machine_ip:
                machine_ip_data.update({
                    ip_field: str(machine_ip),
                    '_'.join((ip_field, 'rdns')): get_rdns_lookup(str(machine_ip)),
                    '_'.join((ip_field, 'subnet')): str(machine_subnet)})
            for field in fields:
                if field not in machine_ip_data:
                    machine_ip_data[field] = NO_DATA
        return machine_ip_data

    @staticmethod
    def merge_machine_ip(old_machine, new_machine):
        for ip_field, fields in MACHINE_IP_FIELDS.items():
            ip = new_machine.get(ip_field, None)
            old_ip = old_machine.get(ip_field, None)
            if not ip and old_ip:
                new_machine[ip_field] = old_ip
                for field in fields:
                    if field in old_machine:
                        new_machine[field] = old_machine[field]

    def find_new_machines(self, machines):
        '''parse switch structure to find new machines added to network
        since last call'''
        change_acls = False

        for machine in machines:
            machine['ether_vendor'] = get_ether_vendor(
                machine['mac'], '/poseidon/poseidon/metadata/nmap-mac-prefixes.txt')
            machine.update(self._parse_machine_ip(machine))
            if not 'controller_type' in machine:
                machine.update({
                    'controller_type': 'none',
                    'controller': ''})
            trunk = False
            for sw in self.trunk_ports:
                if sw == machine['segment'] and self.trunk_ports[sw].split(',')[1] == str(machine['port']) and self.trunk_ports[sw].split(',')[0] == machine['mac']:
                    trunk = True

            h = Endpoint.make_hash(machine, trunk=trunk)
            ep = self.endpoints.get(h, None)
            if ep is None:
                change_acls = True
                m = endpoint_factory(h)
                m.p_prev_states.append((m.state, int(time.time())))
                m.endpoint_data = deepcopy(machine)
                self.endpoints[m.name] = m
                self.logger.info(
                    'Detected new endpoint: {0}:{1}'.format(m.name, machine))
            else:
                self.merge_machine_ip(ep.endpoint_data, machine)

            if ep and ep.endpoint_data != machine and not ep.ignore:
                diff_txt = self._diff_machine(ep.endpoint_data, machine)
                self.logger.info(
                    'Endpoint changed: {0}:{1}'.format(h, diff_txt))
                change_acls = True
                ep.endpoint_data = deepcopy(machine)
                if ep.state == 'inactive' and machine['active'] == 1:
                    if ep.p_next_state in ['known', 'abnormal']:
                        ep.trigger(ep.p_next_state)
                    else:
                        ep.unknown()
                    ep.p_prev_states.append((ep.state, int(time.time())))
                elif ep.state != 'inactive' and machine['active'] == 0:
                    if ep.state in ['mirroring', 'reinvestigating']:
                        status = Actions(
                            ep, self.sdnc).unmirror_endpoint()
                        if not status:
                            self.logger.warning(
                                'Unable to unmirror the endpoint: {0}'.format(ep.name))
                        if ep.state == 'mirroring':
                            ep.p_next_state = 'mirror'
                        elif ep.state == 'reinvestigating':
                            ep.p_next_state = 'reinvestigate'
                    if ep.state in ['known', 'abnormal']:
                        ep.p_next_state = ep.state
                    ep.inactive()
                    ep.p_prev_states.append((ep.state, int(time.time())))

        if change_acls and self.controller['AUTOMATED_ACLS']:
            status = Actions(None, self.sdnc).update_acls(
                rules_file=self.controller['RULES_FILE'],
                endpoints=self.endpoints.values())
            if isinstance(status, list):
                self.logger.info(
                    'Automated ACLs did the following: {0}'.format(status[1]))
                for item in status[1]:
                    machine = {'mac': item[1],
                               'segment': item[2], 'port': item[3]}
                    h = Endpoint.make_hash(machine)
                    ep = self.endpoints.get(h, None)
                    if ep:
                        ep.acl_data.append(
                            (item[0], item[4], item[5]), int(time.time()))
        self.store_endpoints()
        self.get_stored_endpoints()

    def store_endpoints(self):
        ''' store current endpoints in Redis. '''
        with self.redis_lock:
            if self.r:
                try:
                    serialized_endpoints = []
                    for endpoint in self.endpoints.values():
                        # set metadata
                        mac_addresses, ipv4_addresses, ipv6_addresses = self.get_stored_metadata(
                            str(endpoint.name))
                        endpoint.metadata = {
                            'mac_addresses': mac_addresses,
                            'ipv4_addresses': ipv4_addresses,
                            'ipv6_addresses': ipv6_addresses}
                        redis_endpoint_data = {
                            'name': str(endpoint.name),
                            'state': str(endpoint.state),
                            'ignore': str(endpoint.ignore),
                            'endpoint_data': str(endpoint.endpoint_data),
                            'next_state': str(endpoint.p_next_state),
                            'prev_states': str(endpoint.p_prev_states),
                            'acl_data': str(endpoint.acl_data),
                            'metadata': str(endpoint.metadata),
                        }
                        self.r.hmset(endpoint.name, redis_endpoint_data)
                        mac = endpoint.endpoint_data['mac']
                        self.r.hmset(mac, {'poseidon_hash': str(endpoint.name)})
                        if not self.r.sismember('mac_addresses', mac):
                            self.r.sadd('mac_addresses', mac)
                        for ip_field in MACHINE_IP_FIELDS:
                            try:
                                machine_ip = ipaddress.ip_address(
                                    endpoint.endpoint_data.get(ip_field, None))
                            except ValueError:
                                machine_ip = None
                            if machine_ip:
                                self.r.hmset(
                                    str(machine_ip), {'poseidon_hash': str(endpoint.name)})
                                if not self.r.sismember('ip_addresses', str(machine_ip)):
                                    self.r.sadd('ip_addresses', str(machine_ip))
                        serialized_endpoints.append(endpoint.encode())
                    self.r.set('p_endpoints', str(serialized_endpoints))
                except Exception as e:  # pragma: no cover
                    self.logger.error(
                        'Unable to store endpoints in Redis because {0}'.format(str(e)))


class Monitor(object):

    def __init__(self, skip_rabbit):
        self.faucet_event = []
        self.m_queue = queue.Queue()
        self.skip_rabbit = skip_rabbit
        self.logger = logger
        self.rabbit_channel_connection_local = None
        self.rabbit_channel_connection_local_fa = None

        # get config options
        self.controller = Config().get_config()

        # timer class to call things periodically in own thread
        self.schedule = schedule

        # setup prometheus
        self.prom = Prometheus()
        try:
            self.prom.initialize_metrics()
        except Exception as e:  # pragma: no cover
            self.logger.debug(
                'Prometheus metrics are already initialized: {0}'.format(str(e)))
        Prometheus.start()

        # initialize sdnconnect
        self.s = SDNConnect(self.controller)

        # schedule periodic scan of endpoints thread
        self.schedule.every(self.controller['scan_frequency']).seconds.do(
            partial(schedule_job_kickurl, schedule_func=self))

        # schedule periodic reinvestigations thread
        self.schedule.every(self.controller['reinvestigation_frequency']).seconds.do(
            partial(schedule_job_reinvestigation, schedule_func=self))

        # schedule all threads
        self.schedule_thread = threading.Thread(
            target=partial(
                schedule_thread_worker,
                schedule=self.schedule),
            name='st_worker')

    def format_rabbit_message(self, item):
        '''
        read a message off the rabbit_q
        the message should be item = (routing_key,msg)
        '''
        ret_val = {}

        routing_key, my_obj = item
        self.logger.debug('rabbit_message:{0}'.format(my_obj))
        my_obj = json.loads(my_obj)
        self.logger.debug('routing_key:{0}'.format(routing_key))
        remove_list = []

        if routing_key == 'poseidon.algos.decider':
            self.logger.debug('decider_ value:{0}'.format(my_obj))
            for name, message in my_obj.items():
                self.logger.debug('decider_ iteration name: {0}, message: {1}'.format(name, message))
                endpoint = self.s.endpoints.get(name, None)
                self.logger.debug('decider_ endpoint: {0}'.format(json.dumps(endpoint, indent=2)))
                self.logger.debug('decider_ plugin: {0}'.format(message.get('plugin', None)))
                self.logger.debug('decider_ valid: {0}'.format(message.get('valid', False)))
                if endpoint and message.get('plugin', None) == 'ncapture':
                    endpoint.trigger('unknown')
                    endpoint.p_next_state = None
                    endpoint.p_prev_states.append(
                        (endpoint.state, int(time.time())))
                    if message.get('valid', False):
                        self.logger.debug('decider_ updating: {0}'.format(message.get('plugin', None)))
                        ret_val.update(my_obj)
                    else:
                        ret_val = {}
                        break
            self.logger.debug('decider_ retval: {0}'.format(ret_val))    
        elif routing_key == 'poseidon.action.ignore':
            for name in my_obj:
                endpoint = self.s.endpoints.get(name, None)
                if endpoint:
                    endpoint.ignore = True
        elif routing_key == 'poseidon.action.clear.ignored':
            for name in my_obj:
                endpoint = self.s.endpoints.get(name, None)
                if endpoint:
                    endpoint.ignore = False
        elif routing_key == 'poseidon.action.change':
            for name, state in my_obj:
                endpoint = self.s.endpoints.get(name, None)
                if endpoint:
                    try:
                        if state != 'mirror' and state != 'reinvestigate' and (endpoint.state == 'mirroring' or endpoint.state == 'reinvestigating'):
                            status = Actions(
                                endpoint, self.s.sdnc).unmirror_endpoint()
                            if not status:
                                self.logger.warning(
                                    'Unable to unmirror the endpoint: {0}'.format(endpoint.name))
                        endpoint.trigger(state)
                        endpoint.p_next_state = None
                        endpoint.p_prev_states.append(
                            (endpoint.state, int(time.time())))
                        if endpoint.state == 'mirroring' or endpoint.state == 'reinvestigating':
                            status = Actions(
                                endpoint, self.s.sdnc).mirror_endpoint()
                            if status:
                                try:
                                    self.s.r.hincrby(
                                        'vent_plugin_counts', 'ncapture')
                                except Exception as e:  # pragma: no cover
                                    self.logger.error(
                                        'Failed to update count of plugins because: {0}'.format(str(e)))
                            else:
                                self.logger.warning(
                                    'Unable to mirror the endpoint: {0}'.format(endpoint.name))
                    except Exception as e:  # pragma: no cover
                        self.logger.error(
                            'Unable to change endpoint {0} because: {1}'.format(endpoint.name, str(e)))
        elif routing_key == 'poseidon.action.update_acls':
            for ip in my_obj:
                rules = my_obj[ip]
                endpoint = self.s.endpoints_by_ip(ip)
                if endpoint:
                    try:
                        status = Actions(
                            endpoint, self.s.sdnc).update_acls(rules_file=self.controller['rules_file'], endpoints=endpoint, force_apply_rules=rules)
                        if not status:
                            self.logger.warning(
                                'Unable to apply rules: {0} to endpoint: {1}'.format(rules, endpoint.name))
                    except Exception as e:
                        self.logger.error(
                                'Unable to apply rules: {0} to endpoint: {1} because {2}'.format(rules, endpoint.name, str(e)))
        elif routing_key == 'poseidon.action.remove':
            remove_list = [name for name in my_obj]
        elif routing_key == 'poseidon.action.remove.ignored':
            remove_list = [
                endpoint.name for endpoint in self.s.endpoints.values() if endpoint.ignore]
        elif routing_key == 'poseidon.action.remove.inactives':
            remove_list = [endpoint.name for endpoint in self.s.endpoints.values(
            ) if endpoint.state == 'inactive']
        elif routing_key == self.controller['FA_RABBIT_ROUTING_KEY']:
            self.logger.debug('FAUCET Event:{0}'.format(my_obj))
            ret_val.update(my_obj)
        for endpoint_name in remove_list:
            if endpoint_name in self.s.endpoints:
                del self.s.endpoints[endpoint_name]
        return ret_val

    def process(self):
        global CTRL_C
        signal.signal(signal.SIGINT, partial(self.signal_handler))
        while not CTRL_C['STOP']:
            time.sleep(1)

            found_work, item = self.get_q_item()
            ml_returns = {}

            if found_work and item[0] == self.controller['FA_RABBIT_ROUTING_KEY']:
                self.faucet_event.append(self.format_rabbit_message(item))
                self.logger.debug(
                    'Faucet event: {0}'.format(self.faucet_event))
            elif found_work:
                msg = self.format_rabbit_message(item)
                self.logger.debug('decider_ output: {0}'.format(json.dumps(msg)))
                if 'data' in msg:
                    ml_returns = msg['data']
                if ml_returns:
                    self.logger.info(
                        'ML results: {0}'.format(ml_returns))
                extras = deepcopy(ml_returns)
                # process results from ml output and update impacted endpoints
                for ep in self.s.endpoints.values():
                    if ep.name in ml_returns:
                        del extras[ep.name]
                    if ep.name in ml_returns and 'valid' in ml_returns[ep.name] and not ep.ignore:
                        if ep.state in ['mirroring', 'reinvestigating']:
                            status = Actions(
                                ep, self.s.sdnc).unmirror_endpoint()
                            if not status:
                                self.logger.warning(
                                    'Unable to unmirror the endpoint: {0}'.format(ep.name))
                        if ml_returns[ep.name]['valid']:
                            ml_decision = None
                            if 'decisions' in ml_returns[ep.name] and 'behavior' in ml_returns[ep.name]['decisions']:
                                ml_decision = ml_returns[ep.name]['decisions']['behavior']
                            if ml_decision == 'normal':
                                ep.known()
                            else:
                                ep.abnormal()
                        else:
                            ep.unknown()
                        ep.p_prev_states.append(
                            (ep.state, int(time.time())))
                extra_machines = []
                self.logger.debug('extra devices: {0}'.format(extras))
                for device in extras:
                    if device['valid']:
                        extra_machine = {
                            'mac': device['source_mac'],
                            'segment': NO_DATA,
                            'port': NO_DATA,
                            'tenant': NO_DATA,
                            'active': 0,
                            'name': None}
                        try:
                            source_ip = ipaddress.ip_address(
                                device['source_ip'])
                        except ValueError:
                            source_ip = None
                        if source_ip:
                            extra_machine['ipv%u' %
                                          source_ip.version] = str(source_ip)
                        extra_machines.append(extra_machine)
                self.s.find_new_machines(extra_machines)

            queued_endpoints = [
                endpoint for endpoint in self.s.endpoints.values()
                if not endpoint.ignore and endpoint.state == 'queued' and endpoint.p_next_state != 'inactive']
            self.s.investigations = len([
                endpoint for endpoint in self.s.endpoints.values()
                if endpoint.state in ['mirroring', 'reinvestigating']])
            # mirror things in the order they got added to the queue
            queued_endpoints = sorted(
                queued_endpoints, key=lambda x: x.p_prev_states[-1][1])

            investigation_budget = max(
                self.controller['max_concurrent_reinvestigations'] -
                self.s.investigations,
                0)
            self.logger.debug('investigations {0}, budget {1}, queued {2}'.format(
                str(self.s.investigations), str(investigation_budget), str(len(queued_endpoints))))

            for endpoint in queued_endpoints[:investigation_budget]:
                endpoint.trigger(endpoint.p_next_state)
                endpoint.p_next_state = None
                endpoint.p_prev_states.append(
                    (endpoint.state, int(time.time())))
                status = Actions(
                    endpoint, self.s.sdnc).mirror_endpoint()
                if status:
                    try:
                        if self.s.r:
                            self.s.r.hincrby('vent_plugin_counts', 'ncapture')
                    except Exception as e:  # pragma: no cover
                        self.logger.error(
                            'Failed to update count of plugins because: {0}'.format(str(e)))
                else:
                    self.logger.warning(
                        'Unable to mirror the endpoint: {0}'.format(endpoint.name))

            for endpoint in self.s.endpoints.values():
                if not endpoint.ignore:
                    if self.s.sdnc:
                        if endpoint.state == 'unknown':
                            endpoint.p_next_state = 'mirror'
                            endpoint.queue()
                            endpoint.p_prev_states.append(
                                (endpoint.state, int(time.time())))
                        elif endpoint.state in ['mirroring', 'reinvestigating']:
                            cur_time = int(time.time())
                            # timeout after 2 times the reinvestigation frequency
                            # in case something didn't report back, put back in an
                            # unknown state
                            if cur_time - endpoint.p_prev_states[-1][1] > 2*self.controller['reinvestigation_frequency']:
                                self.logger.debug(
                                    'timing out: {0} and setting to unknown'.format(endpoint.name))
                                status = Actions(
                                    endpoint, self.s.sdnc).unmirror_endpoint()
                                if not status:
                                    self.logger.warning(
                                        'Unable to unmirror the endpoint: {0}'.format(endpoint.name))
                                endpoint.unknown()
                                endpoint.p_prev_states.append(
                                    (endpoint.state, int(time.time())))
                    else:
                        if endpoint.state != 'known':
                            endpoint.known()
        self.s.store_endpoints()
        return

    def get_q_item(self):
        '''
        attempt to get a work item from the queue
        m_queue -> (routing_key, body)
        a read from get_q_item should be of the form
        (boolean,(routing_key, body))
        '''
        found_work = False
        item = None
        global CTRL_C

        if not CTRL_C['STOP']:
            try:
                item = self.m_queue.get(False)
                found_work = True
                self.m_queue.task_done()
            except queue.Empty:  # pragma: no cover
                pass

        return (found_work, item)

    def shutdown(self):
        ''' gracefully shut down. '''
        self.s.clear_filters()
        for job in self.schedule.jobs:
            self.logger.debug('shutdown :{0}'.format(job))
            self.schedule.cancel_job(job)
        if self.rabbit_channel_connection_local:
            self.rabbit_channel_connection_local.close()
        if self.rabbit_channel_connection_local_fa:
            self.rabbit_channel_connection_local_fa.close()
        self.logger.debug('SHUTTING DOWN')
        self.logger.debug('EXITING')
        sys.exit()

    def signal_handler(self, signal, frame):
        ''' hopefully eat a CTRL_C and signal system shutdown '''
        global CTRL_C
        CTRL_C['STOP'] = True
        self.logger.debug('CTRL-C: {0}'.format(CTRL_C))
        try:
            self.shutdown()
        except Exception as e:  # pragma: no cover
            self.logger.debug(
                'Failed to handle signal properly because: {0}'.format(str(e)))


def main(skip_rabbit=False):  # pragma: no cover
    # setup rabbit and monitoring of the network
    pmain = Monitor(skip_rabbit=skip_rabbit)
    if not skip_rabbit:
        rabbit = Rabbit()
        host = pmain.controller['rabbit_server']
        port = int(pmain.controller['rabbit_port'])
        exchange = 'topic-poseidon-internal'
        queue_name = 'poseidon_main'
        binding_key = ['poseidon.algos.#', 'poseidon.action.#']
        retval = rabbit.make_rabbit_connection(
            host, port, exchange, queue_name, binding_key)
        pmain.rabbit_channel_local = retval[0]
        pmain.rabbit_channel_connection_local = retval[1]
        pmain.rabbit_thread = rabbit.start_channel(
            pmain.rabbit_channel_local,
            rabbit_callback,
            queue_name,
            pmain.m_queue)

    if pmain.controller['FA_RABBIT_ENABLED']:
        rabbit = Rabbit()
        host = pmain.controller['FA_RABBIT_HOST']
        port = pmain.controller['FA_RABBIT_PORT']
        exchange = pmain.controller['FA_RABBIT_EXCHANGE']
        queue_name = 'poseidon_main'
        binding_key = [pmain.controller['FA_RABBIT_ROUTING_KEY']+'.#']
        retval = rabbit.make_rabbit_connection(
            host, port, exchange, queue_name, binding_key)
        pmain.rabbit_channel_local = retval[0]
        pmain.rabbit_channel_connection_local_fa = retval[1]
        pmain.rabbit_thread = rabbit.start_channel(
            pmain.rabbit_channel_local,
            rabbit_callback,
            queue_name,
            pmain.m_queue)

    pmain.schedule_thread.start()

    # loop here until told not to
    try:
        pmain.process()
    except Exception as e:
        logger.error('process() exception: {0}'.format(str(e)))

    pmain.shutdown()


if __name__ == '__main__':  # pragma: no cover
    main(skip_rabbit=False)
