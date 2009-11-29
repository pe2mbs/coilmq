#
# Copyright 2009 Hans Lellelid
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# 
#   http://www.apache.org/licenses/LICENSE-2.0
# 
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Queue manager, queue implementation, and supporting classes.

This code is inspired by the design of the Ruby stompserver project, by 
Patrick Hurley and Lionel Bouton.  See http://stompserver.rubyforge.org/
"""
__authors__ = [
  '"Hans Lellelid" <hans@xmpl.org>',
]

import logging
import threading
import uuid

from collections import defaultdict

from coilmq.scheduler import FavorReliableSubscriberScheduler, RandomQueueScheduler
from coilmq.util.concurrency import synchronized

class QueueManager(object):
    """
    Class that manages distribution of messages to queue subscribers.
    
    This class uses C{threading.RLock} to guard the public methods.  This is probably
    a bit excessive, given 1) the actomic nature of basic C{dict} read/write operations 
    and  2) the fact that most of the internal data structures are keying off of the 
    STOMP connection, which is going to be thread-isolated.  That said, this seems like 
    the technically correct approach and should increase the chance of this code being
    portable to non-GIL systems. 
    
    @ivar store: The queue storage backend to use.
    @type store: L{coilmq.store.QueueStore}
        
    @ivar subscriber_scheduler: The scheduler that chooses which subscriber to send
                                    messages to.
    @type subscriber_scheduler: L{coilmq.scheduler.SubscriberPriorityScheduler}
        
    @ivar queue_scheduler: The scheduler that chooses which queue to select for sending
                                    backlogs for a single connection.
    @type queue_scheduler: L{coilmq.scheduler.QueuePriorityScheduler}
    
    @ivar _queues: A dict of registered queues, keyed by destination.
    @type _queues: C{dict} of C{str} to C{set} of L{coilmq.server.StompConnection}
        
    @ivar _pending: All messages waiting for ACK from clients.
    @type _pending: C{dict} of L{coilmq.server.StompConnection} to L{coilmq.frame.StompFrame}
    
    @ivar _transaction_frames: Frames that have been ACK'd within a transaction.
    @type _transaction_frames: C{dict} of L{coilmq.server.StompConnection} to C{dict} of C{str} to L{coilmq.frame.StompFrame}
    """
    
    def __init__(self, store, subscriber_scheduler=None, queue_scheduler=None):
        """
        @param store: The queue storage backend.
        @type store: L{coilmq.store.QueueStore}
        
        @param subscriber_scheduler: The scheduler that chooses which subscriber to send
                                    messages to.
        @type subscriber_scheduler: L{coilmq.scheduler.SubscriberPriorityScheduler}
        
        @param queue_scheduler: The scheduler that chooses which queue to select for sending
                                    backlogs for a single connection.
        @type queue_scheduler: L{coilmq.scheduler.QueuePriorityScheduler}
        """
        self.log = logging.getLogger('%s.%s' % (__name__, self.__class__.__name__))
        
        
        # Use default schedulers, if they're not specified
        if subscriber_scheduler is None:
            subscriber_scheduler = FavorReliableSubscriberScheduler()
        
        if queue_scheduler is None:
            queue_scheduler = RandomQueueScheduler()
            
        # This lock var is required by L{synchronized} decorator.
        self._lock = threading.RLock()
        
        self.store = store
        self.subscriber_scheduler = subscriber_scheduler
        self.queue_scheduler = queue_scheduler
        
        self._queues = defaultdict(set)
        self._transaction_frames = defaultdict(lambda: defaultdict(list))
        self._pending = {}
    
    @synchronized
    def subscribe(self, connection, destination):
        """
        Subscribes a connection to the specified destination (topic or queue). 
        
        @param connection: The connection to subscribe.
        @type connection: L{coilmq.server.StompConnection}
        
        @param destination: The topic/queue destination (e.g. '/queue/foo')
        @type destination: C{str} 
        """
        self.log.debug("Subscribing %s to %s" % (connection, destination))
        self._queues[destination].add(connection)
        self._send_backlog(connection, destination)
    
    @synchronized
    def unsubscribe(self, connection, destination):
        """
        Unsubscribes a connection from a destination (topic or queue).
        
        @param connection: The client connection to unsubscribe.
        @type connection: L{coilmq.server.StompConnection}
        
        @param destination: The topic/queue destination (e.g. '/queue/foo')
        @type destination: C{str} 
        """
        self.log.debug("Unsubscribing %s from %s" % (connection, destination))
        if connection in self._queues[destination]:
            self._queues[destination].remove(connection)
        
        if not self._queues[destination]:
            del self._queues[destination]
    
    @synchronized
    def disconnect(self, connection):
        """
        Removes a subscriber connection, ensuring that any pending commands get requeued.
        
        @param connection: The client connection to unsubscribe.
        @type connection: L{coilmq.server.StompConnection}
        """
        self.log.debug("Disconnecting %s" % connection)
        if connection in self._pending:
            self.store.requeue(self._pending[connection])
            del self._pending[connection]
        
        for dest in self._queues.keys():
            if connection in self._queues[dest]:
                self._queues[dest].remove(connection)
            if not self._queues[dest]:
                del self._queues[dest] # This won't trigger RuntimeError, since we're using keys()
    
    
    @synchronized
    def send(self, message):
        """
        Sends a MESSAGE frame to an eligible subscriber connection.
        
        Note that this method will modify the incoming message object to 
        add a message-id header (if not present) and to change the command
        to 'MESSAGE' (if it is not).
         
        @param message: The message frame.
        @type message: L{coilmq.frame.StompFrame}
        """
        dest = message.destination
        if not dest:
            raise ValueError("Cannot send frame with no destination: %s" % message)
        
        message.cmd = 'MESSAGE'
        
        if not 'message-id' in message.headers:
            message.headers['message-id'] = str(uuid.uuid4())
        
        # Grab all subscribers for this destination that do not have pending frames
        subscribers = [s for s in self._queues[dest] 
                                    if s not in self._pending]
        
        if not subscribers:
            self.store.enqueue(dest, message)
        else:
            selected = self.subscriber_scheduler.choice(subscribers, message)
            self._send_frame(selected, message)
    
    @synchronized
    def ack(self, connection, frame, transaction=None):
        """
        Acknowledge receipt of a message.
        
        If the `transaction` parameter is non-null, the frame being ack'd
        will be queued so that it can be requeued if the transaction
        is rolled back. 
        
        @param connection: The connection that is acknowledging the frame.
        @type connection: L{coilmq.server.StompConnection}
        
        @param frame: The frame being acknowledged.
        
        """
        self.log.debug("ACK %s for %s" % (frame, connection))
        
        if connection in self._pending:
            pending_frame = self._pending[connection]
            # Make sure that the frame being acknowledged matches
            # the expected frame
            if pending_frame.message_id != frame.message_id:
                self.log.warning("Got a ACK for unexpected message-id: %s" % frame.message_id)
                self.store.requeue(pending_frame.destination, pending_frame)
                # (The pending frame will be removed further down)
            
            if transaction is not None:
                self._transaction_frames[connection][transaction].append(pending_frame)
            
            del self._pending[connection]            
            self._send_subscriber_backlog(connection)
            
        else:
            self.log.debug("No pending messages for %s" % connection)
    
    @synchronized
    def resend_transaction_frames(self, connection, transaction):
        """
        Resend the messages that were ACK'd in specified transaction.
        
        This is called by the engine when there is an abort command.
        
        @param connection: The client connection that aborted the transaction.
        @type connection: L{coilmq.server.StompConnection}
        
        @param transaction: The transaction id (which was aborted).
        @type transaction: C{str}
        """
        for frame in self._transaction_frames[connection][transaction]:
            self.send(frame)
    
    @synchronized
    def clear_transaction_frames(self, connection, transaction):
        """
        Clears out the queued ACK frames for specified transaction. 
        
        This is called by the engine when there is a commit command.
        
        @param connection: The client connection that committed the transaction.
        @type connection: L{coilmq.server.StompConnection}
        
        @param transaction: The transaction id (which was committed).
        @type transaction: C{str}
        """
        
        del self._transaction_frames[connection][transaction]
        
    def _send_backlog(self, connection, destination):
        """
        Sends any queued-up messages for the specified destination to connection.
        
        This is called when new subscribers are added to the system.
        
        (This method assumes it is being called from within a lock-guarded public
        method.)  
        
        @param connection: The client connection.
        @type connection: L{coilmq.server.StompConnection}
        
        @param destination: The topic/queue destination (e.g. '/queue/foo')
        @type destination: C{str} 
        """ 
        self.log.debug("Sending backlog to %s for destination %s" % (connection, destination))
        if connection.reliable_subscriber:
            # only send one message (waiting for ack)
            frame = self.store.dequeue(destination)
            if frame:
                self._send_frame(connection, frame)
        else:
            for frame in self.store.frames(destination):
                self._send_frame(connection, frame)
                
    def _send_subscriber_backlog(self, connection):
        """
        Sends waiting message(s) for a single subscriber.
        
        (This method assumes it is being called from within a lock-guarded public
        method.)
        
        @param connection: The client connection.
        @type connection: L{coilmq.server.StompConnection}
        """
        # Find all destinations that have frames and that contain this
        # connection (subscriber).
        eligible_queues = dict([(dest,q) for (dest, q) in self._queues.items()
                            if connection in q and self.store.has_frames(dest)])
        
        if not eligible_queues:
            self.log.debug("No eligible queues for connection %s" % connection)
            return
        
        selected = self.queue_scheduler.choice(eligible_queues, connection)
        if selected:
            frame = self.store.dequeue(selected)
            if frame:
                self._send_frame(connection, frame)
                
    def _send_frame(self, connection, frame):
        """
        Sends a frame to a specific subscriber connection.
        
        (This method assumes it is being called from within a lock-guarded public
        method.)
        
        @param connection: The subscriber connection object to send to.
        @type connection: L{coilmq.server.StompConnection}
        
        @param frame: The frame to send.
        @type frame: L{coilmq.frame.StompFrame}
        """
        assert connection is not None
        assert frame is not None
        
        if connection.reliable_subscriber:
            if connection in self._pending:
                raise RuntimeError("Connection already has a pending frame.")
            self.log.debug("Adding pending frame %s to connection %s" % (frame, connection))
            self._pending[connection] = frame
            
        connection.send_frame(frame)