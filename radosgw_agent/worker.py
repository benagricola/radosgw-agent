from collections import namedtuple
import logging
import multiprocessing
import requests
import os
import socket

from radosgw_agent import client
from radosgw_agent import lock

log = logging.getLogger(__name__)

RESULT_SUCCESS = 0
RESULT_ERROR = 1
RESULT_CONNECTION_ERROR = 2
MAX_CONCURRENT_OPS = 5

class Worker(multiprocessing.Process):
    """sync worker to run in its own process"""

    def __init__(self, work_queue, result_queue, log_lock_time,
                 src, dest, **kwargs):
        super(Worker, self).__init__()
        self.source_zone = src.zone
        self.dest_zone = dest.zone
        self.work_queue = work_queue
        self.result_queue = result_queue
        self.log_lock_time = log_lock_time
        self.lock = None

        self.local_lock_id = socket.gethostname() + str(os.getpid())

        # construct the two connection objects
        self.source_conn = client.connection(src)
        self.dest_conn = client.connection(dest)

    def prepare_lock(self):
        assert self.lock is None
        self.lock = lock.Lock(self.source_conn, self.type, self.local_lock_id,
                              self.log_lock_time, self.source_zone)
        self.lock.daemon = True
        self.lock.start()

MetadataEntry = namedtuple('MetadataEntry',
                           ['section', 'name', 'marker', 'timestamp'])

def _meta_entry_from_json(entry):
    return MetadataEntry(
        entry['section'],
        entry['name'],
        entry['id'],
        entry['timestamp'],
        )

class DataWorker(Worker):

    def __init__(self, *args, **kwargs):
        super(DataWorker, self).__init__(*args, **kwargs)
        self.type = 'data'

    def get_new_op_id(self):
        self.op_id = self.op_id + 1
        return self.op_id

    def sync_object(self, connection, bucket, key, src_zone, client_id):
        op_id = self.get_new_op_id()
        client.sync_object_intra_region(connection, bucket, key, src_zone, client_id, op_id)
        return op_id

    # TODO
    # use the op_id to keep track of on-going copies
    def sync_data(self, bucket_name):
        log.debug('syncing bucket {bucket}'.format(bucket=bucket_name))

        objects = client.list_objects_in_bucket(self.source_conn, bucket_name)
        counter = 0

        # sync each object in the list.
        # We only want to have X in flight at once, so use the op_id to track when 
        # individual operations finish.  
        inflight_ops = []
        for key in objects:
            counter = counter + 1
            # sync each object
            log.debug('syncing object {bucket}:{key}'.format(bucket=bucket_name,key=key))
            op_id = self.sync_object(self.dest_conn, key.bucket.name, key.name, self.source_zone, self.daemon_id)
            inflight_ops.append(op_id)
            # Do not progress to the next key until there are less than the maximum
            # number of syncs in-flight
            #while(len(inflight_ops) == MAX_CONCURRENT_OPS):
                # check each of the inflight ops. If they've been completed, remove them from the in-flight list
                #for op_id in inflight_ops:

        # Once all the copy commands are issued, track op_ids until they're all done
        for op_id in inflight_ops:
            op_ids = client.list_ops_for_client(self.dest_conn, self.daemon_id, op_id)
            log.debug('jbuck, op_ids: {op_ids}'.format(op_ids=op_ids))

        log.debug('bucket {bucket} has {num_objects} object'.format(
                  bucket=bucket_name,num_objects=counter))

class DataWorkerIncremental(DataWorker):

    def __init__(self, *args, **kwargs):
        self.daemon_id = kwargs['daemon_id']
        self.max_entries = kwargs['max_entries']
        self.op_id = 0
        super(DataWorkerIncremental, self).__init__(*args, **kwargs)

    def get_and_process_entries(self, marker, shard_num):
        pass

    def _get_and_process_entries(self, marker, shard_num):
        pass

    def run(self):
        pass

class DataWorkerFull(DataWorker):

    def __init__(self, *args, **kwargs):
        self.daemon_id = kwargs['daemon_id']
        self.op_id = 0
        super(DataWorkerFull, self).__init__(*args, **kwargs)

    def run(self):
        self.prepare_lock()
        num_data_shards = client.num_log_shards(self.source_conn, 'data')

        while True:
            shard_num = self.work_queue.get()
            if shard_num is None:
                log.info('No more entries in queue, exiting')
                break

            log.info('%s is processing shard %d', self.ident, shard_num)

            # lock the log
            try:
                self.lock.set_shard(shard_num)
                self.lock.acquire()
            except client.NotFound:
                self.lock.unset_shard()
                self.result_queue.put((RESULT_SUCCESS, shard_num))
                continue
            except client.HttpError as e:
                log.info('error locking shard %d log, assuming'
                         ' it was processed by someone else and skipping: %s',
                         shard_num, e)
                self.lock.unset_shard()
                self.result_queue.put((RESULT_ERROR, shard_num))
                continue

            # set a marker in the replica log 
            worker_bound_info = None
            buckets_to_sync = []
            try:
                worker_bound_info = client.get_worker_bound(self.dest_conn, 'data', shard_num=shard_num)
                log.debug('data full sync shard {i} data log is {data}'.format(i=shard_num,data=worker_bound_info))
                # determine whether there's a marker string with NEEDSSYNC and with 
                # our daemon_id. If so, sync it the bucket(s) that match
            except Exception as e:
                log.exception('could not set worker bound for shard_num %d: %s',
                              shard_num, e)
                self.lock.unset_shard()
                self.result_queue.put((RESULT_ERROR, shard_num))
                continue

            # set the default result
            result = RESULT_SUCCESS


            log_bucket_name = ""
#            try:
#               for bucket_name in buckets_to_sync:
#                   log.info('bucket %s is processed as part of shard %d', bucket_name, shard_num)
#                   self.sync_data(bucket_name)
#
#                result = RESULT_SUCCESS
#            except Exception as e:
#                log.exception('could not sync shard_num %d failed on bucket %s: %s',
#                              shard_num, log_bucket_name, e)
#                self.lock.unset_shard()
#                self.result_queue.put((RESULT_ERROR, shard_num))
#                continue
#
#            # TODO this may need to do a set the omits the synced buckets and then delete the entries for the 
#            # synced buckets? The replica log usage is still pretty ill-defined
#            # remove the data replica log entry. If we have gotten to this part, all 
#            # the pertinent info should be valid.
#            try:
#                client.del_worker_bound(self.dest_conn, 'data', shard_num,
#                                        self.daemon_id)
#            except Exception as e:
#                log.exception('could not delete worker bound for shard_num %d: %s',
#                              shard_num, e)
#                self.lock.unset_shard()
#                self.result_queue.put((RESULT_ERROR, shard_num))
#                continue

            # TODO
            # update the bucket index log (trim it)

            # finally, unlock the log
            try:
                self.lock.release_and_clear()
            except lock.LockBroken as e:
                log.warn('work may be duplicated: %s', e)
            except:
                log.exception('error unlocking data log, continuing anyway '
                              'since lock will timeout')

            self.result_queue.put((result, shard_num))

class MetadataWorker(Worker):

    def __init__(self, *args, **kwargs):
        super(MetadataWorker, self).__init__(*args, **kwargs)
        self.type = 'metadata'

    def sync_meta(self, section, name):
        log.debug('syncing metadata type %s key %r', section, name)
        try:
            metadata = client.get_metadata(self.source_conn, section, name)
        except client.NotFound:
            log.debug('%s %r not found on master, deleting from secondary',
                      section, name)
            try:
                client.delete_metadata(self.dest_conn, section, name)
            except client.NotFound:
                # Since this error is handled appropriately, return success
                return RESULT_SUCCESS 
        except client.HttpError as e:
            log.error('error getting metadata for %s "%s": %s',
                      section, name, e)
            return RESULT_ERROR             
        else:
            try:
                client.update_metadata(self.dest_conn, section, name, metadata)
                return RESULT_SUCCESS 
            except client.HttpError as e:
                log.error('error getting metadata for %s "%s": %s',
                          section, name, e)
                return RESULT_ERROR             

class MetadataWorkerIncremental(MetadataWorker):

    def __init__(self, *args, **kwargs):
        self.daemon_id = kwargs['daemon_id']
        self.max_entries = kwargs['max_entries']
        super(MetadataWorkerIncremental, self).__init__(*args, **kwargs)

    def get_and_process_entries(self, marker, shard_num):
        num_entries = self.max_entries
        while num_entries >= self.max_entries:
            num_entries, marker = self._get_and_process_entries(marker,
                                                                shard_num)

    def _get_and_process_entries(self, marker, shard_num):
        """
        sync up to self.max_entries entries, returning number of entries
        processed and the last marker of the entries processed.
        """
        log_entries = client.get_log(self.source_conn, 'metadata', shard_num,
                                     marker, self.max_entries)

        log.info('shard %d has %d entries after %r', shard_num, len(log_entries),
                 marker)
        try:
            entries = [_meta_entry_from_json(entry) for entry in log_entries]
        except KeyError:
            log.error('log conting bad key is: %s', log_entries)
            raise

        error_encountered = False
        mentioned = set([(entry.section, entry.name) for entry in entries])
        for section, name in mentioned:
            sync_result = self.sync_meta(section, name)
            if sync_result == RESULT_ERROR:
                error_encountered = True

        # Only set worker bounds if there was data synced and no 
        # errors were encountered
        if entries and not error_encountered:
            try:
                client.set_worker_bound(self.dest_conn, 'metadata',
                                        entries[-1].marker,
                                        entries[-1].timestamp,
                                        self.daemon_id,
                                        shard_num=shard_num)
                return len(entries), entries[-1].marker
            except:
                log.exception('error setting worker bound for shard {shard_num},'
                              ' may duplicate some work later'.format(shard_num=shard_num))
        elif entries and error_encountered:
            log.error('Error encountered while syncing shard {shard_num}.'
                      'Not setting worker bound, may duplicate some work later'.format(shard_num=shard_num))

        return 0, ''

    def run(self):
        self.prepare_lock()
        while True:
            shard_num = self.work_queue.get()
            if shard_num is None:
                log.info('process %s is done. Exiting', self.ident)
                break

            log.info('%s is processing shard number %d',
                     self.ident, shard_num)

            # first, lock the log
            try:
                self.lock.set_shard(shard_num)
                self.lock.acquire()
            except client.NotFound:
                # no log means nothing changed in this time period
                self.lock.unset_shard()
                self.result_queue.put((RESULT_SUCCESS, shard_num))
                continue
            except client.HttpError as e:
                log.exception('error locking shard %d log, assuming'
                         ' it was processed by someone else and skipping: %s',
                         shard_num, e)
                self.lock.unset_shard()
                self.result_queue.put((RESULT_ERROR, shard_num))
                continue
            except requests.exceptions.ConnectionError as e:
                log.exception('ConnectionError encountered. Bailing out of'
                              ' processing loop for shard %d. %s', 
                              shard_num, e)
                self.lock.unset_shard()
                self.result_queue.put((RESULT_CONNECTION_ERROR, shard_num))
                break

            result = RESULT_SUCCESS
            try:
                marker, time = client.get_min_worker_bound(self.dest_conn,
                                                           'metadata',
                                                           shard_num=shard_num)
                log.debug('oldest marker and time for shard %d are: %r %r',
                          shard_num, marker, time)
            except client.NotFound:
                # if no worker bounds have been set, start from the beginning
                marker, time = '', '1970-01-01 00:00:00'
            except requests.exceptions.ConnectionError as e:
                log.exception('ConnectionError encountered. Bailing out of'
                              ' processing loop for shard %d. %s', 
                              shard_num, e)
                self.lock.unset_shard()
                self.result_queue.put((RESULT_CONNECTION_ERROR, shard_num))
                break
            except Exception as e:
                log.exception('error getting worker bound for shard %d',
                              shard_num)
                result = RESULT_ERROR

            try:
                if result == RESULT_SUCCESS:
                    self.get_and_process_entries(marker, shard_num)
            except requests.exceptions.ConnectionError as e:
                log.exception('ConnectionError encountered. Bailing out of'
                              ' processing loop for shard %d. %s', 
                              shard_num, e)
                self.lock.unset_shard()
                self.result_queue.put((RESULT_CONNECTION_ERROR, shard_num))
                break
            except:
                log.exception('syncing entries from %s for shard %d failed',
                              marker, shard_num)
                result = RESULT_ERROR

            # finally, unlock the log
            try:
                self.lock.release_and_clear()
            except lock.LockBroken as e:
                log.warn('work may be duplicated: %s', e)
            except:
                log.exception('error unlocking log, continuing anyway '
                              'since lock will timeout')

            self.result_queue.put((result, shard_num))
            log.info('finished processing shard %d', shard_num)

class MetadataWorkerFull(MetadataWorker):

    def run(self):
        while True:
            meta = self.work_queue.get()
            if meta is None:
                log.info('No more entries in queue, exiting')
                break

            try:
                section, name = meta
                self.sync_meta(section, name)
                result = RESULT_SUCCESS
            except Exception as e:
                log.exception('could not sync entry %s "%s": %s',
                              section, name, e)
                result = RESULT_ERROR
            self.result_queue.put((result, (section, name)))
