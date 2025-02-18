import copy
import functools
import itertools
import traceback
from functools import reduce
from typing import List

from tabulate import tabulate
import jsonpickle

from sawtooth_poet_engine.giskard_block import Block, GiskardBlock, GiskardGenesisBlock
from sawtooth_poet_engine.giskard_message import GiskardMessage
from sawtooth_poet_engine.giskard_nstate import NState
from sawtooth_poet_engine.giskard_node import GiskardNode
import sawtooth_poet_engine.giskard_state_transition_type as giskard_state_transition_type
from sawtooth_poet_engine.giskard_global_state import GState
from sawtooth_poet_engine.giskard_global_trace import GTrace
from sawtooth_poet_tests.integration_tools import BlockCacheMock
from sawtooth_sdk.protobuf.validator_pb2 import Message
from sawtooth_poet.journal.block_wrapper import NULL_BLOCK_IDENTIFIER, LAST_BLOCK_INDEX_IDENTIFIER


class Giskard:
    """The main class for Giskard
        gets called by GiskardEngine for incoming messages """

    # region node methods
    @staticmethod
    def honest_node(node):
        """returns True if the node is honest """
        return not node.dishonest

    @staticmethod
    def is_block_proposer(node, view=0, peers: List[
        str] = []):  # TODO check how to do this with the peers, blocks proposed, view_number, node_id, timeout
        """returns True if the node_id's position in the peers list is equal to the view number % nr of peers"""
        if len(peers) == 1:
            return True
        if isinstance(node, int):
            node = str(node)
        if isinstance(node, str):
            if node == "NotTrustworthy" or node not in peers:
                return False
            position_in_peers = peers.index(node)
        else:
            position_in_peers = peers.index(node.node_id)
        return (position_in_peers + 1) % len(peers) == (view + 1) % len(peers)  # TODO check where view starts 0,1

    @staticmethod
    def get_block_proposer(view: int, peers: List[str]):
        return peers[view % len(peers)]

    # def is_new_proposer_unique TODO write test for that that checks if indeed all views had unique proposers
    # endregion

    # region block methods
    @staticmethod
    def generate_new_block(parent_block: GiskardBlock, block_cache,
                           block_index, node_id) -> GiskardBlock:
        """waits for a new block to be received from the validator """
        if len(block_cache.pending_blocks) == 0:
            return None
        block_cache.pending_blocks.sort(key=lambda b: b.block_num)
        pending_block = block_cache.pending_blocks.pop(0)
        if node_id == "":
            node_id = pending_block.signer_id
        new_block = Block(pending_block.block_id,
                          parent_block.block_id,
                          node_id,
                          pending_block.block_num,
                          pending_block.payload,
                          pending_block.summary)
        return GiskardBlock(new_block, block_index)

    @staticmethod
    def b_last(b: GiskardBlock):
        """Returns True if the block index is the last block in the current view 3 """
        return b.block_index == LAST_BLOCK_INDEX_IDENTIFIER

    @staticmethod
    def generate_last_block(block: GiskardBlock, block_cache, node_id) -> GiskardBlock:
        """TODO still have to figure out if this should be a function or a test """
        return Giskard.generate_new_block(block, block_cache, 3, node_id)

    @staticmethod
    def about_generate_last_block(block: GiskardBlock, block_cache, block_index, node_id) -> bool:
        """Test if the next block to generate would be the last block """
        return Giskard.generate_last_block(block,
                                           block_cache,
                                           node_id).block_height == block.block_height + 1 and Giskard.b_last(
            Giskard.generate_last_block(block, block_cache, node_id))

    @staticmethod
    def about_non_last_block(block: GiskardBlock, block_cache, block_index, node_id) -> bool:
        """Test if the next to be generated block will be the last """
        return not Giskard.b_last(Giskard.generate_new_block(block, block_cache, block_index, node_id))

    @staticmethod
    def parent_of(block: GiskardBlock, block_cache, ignore_block=None) -> GiskardBlock:
        """ Tries to get the parent block from the store
        :param block_cache:
        :param block:
        :return: GiskardBlock, or None """
        if block.block_num == 0:
            return GiskardGenesisBlock()
        return block_cache.block_store.get_parent_block(block, ignore_block)  # check in Store if there is a block with height -1 and the previous id

    @staticmethod
    def parent_ofb(block: GiskardBlock, parent: GiskardBlock, block_cache) -> bool:
        """ Test if parent block relation works with blocks in storage """
        if GiskardGenesisBlock() == parent:
            return True
        parent_cache = Giskard.parent_of(block, block_cache)
        if not parent_cache:
            return False
        if parent_cache != parent:
            """ for the case that there is a same height malicious parent block """
            parent_cache = Giskard.parent_of(block, block_cache, parent_cache)
            if not parent_cache:
                return False
        return parent_cache == parent

    @staticmethod
    def higher_block(b1: GiskardBlock, b2: GiskardBlock) -> GiskardBlock:
        return b1 if (b1.block_num > b2.block_num) else b2

    # endregion

    # region messages and quorum methods
    @staticmethod
    def message_with_higher_block(msg1: GiskardMessage, msg2: GiskardMessage) -> GiskardMessage:
        """ Returns the given message with the higher block """
        Giskard.higher_block(msg1.block, msg2.block)

    @staticmethod
    def has_at_least_two_thirds(nodes, peers) -> bool:
        """ Check if the given nodes are more than a two third majority in the current view """
        matches = [node for node in nodes if node in peers]
        return len(matches) / len(peers) > 2 / 3

    @staticmethod
    def has_at_least_one_third(nodes, peers) -> bool:
        """ Check if the given nodes are at least a third of the peers in the current view """
        matches = [node for node in nodes if node in peers]
        return len(matches) / len(peers) >= 1 / 3

    @staticmethod
    def majority_growth(nodes, node, peers) -> bool:
        """ Is a two third majority reached when this node is added? """
        return Giskard.has_at_least_two_thirds(nodes.append(node), peers)

    @staticmethod
    def majority_shrink(nodes, node, peers) -> bool:
        """ If the given node is removed from nodes, is the two third majority lost? """
        return not Giskard.has_at_least_two_thirds(nodes.remove(node), peers)

    @staticmethod
    def intersection_property(nodes1, nodes2, peers) -> bool:
        """ Don't know if actually needed;
        Checks if the intersection between two lists of nodes, is at least one third of the peers """
        if Giskard.has_at_least_two_thirds(nodes1, peers) \
                and Giskard.has_at_least_two_thirds(nodes2, peers):
            matches = [value for value in nodes1 if value in nodes2]
            return Giskard.has_at_least_one_third(matches, peers)
        return False

    @staticmethod
    def is_member(node, nodes) -> bool:
        """ Checks if a node is actually in a list of nodes """
        if isinstance(node, str):
            if isinstance(nodes[-1], str):
                return node in nodes
            for n in nodes:
                if node == n.node_id:
                    return True
            return False
        return node in nodes

    @staticmethod
    def evil_participants_no_majority(peers) -> bool:
        """ Returns True if there is no majority of dishonest nodes
        TODO make peers of type GiskardNode """
        return not Giskard.has_at_least_one_third(list(filter(lambda peer: peer.dishonest, peers)))

    @staticmethod
    def quorum(lm: List[GiskardMessage], peers) -> bool:
        """ Returns True if a sender from a list of messages has a quorum """
        return Giskard.has_at_least_two_thirds([msg.sender for msg in lm], peers)

    # Don't know if i even need this
    """@staticmethod
        def quorum_subset(self, lm1, lm2):
        if self.quorum(lm1) \
                and self.quorum(lm2):
            matches = [value for value in  if value in nodes2]
            return self.has_at_least_one_third(matches)
        return False """

    @staticmethod
    def quorum_growth(lm: List[GiskardMessage], msg: GiskardMessage, peers) -> bool:
        """ Returns True if a quorum can be reached when a msg is added to a list of messages """
        return Giskard.has_at_least_two_thirds([msg1.sender for msg1 in lm.append(msg)], peers)

    # endregion

    # region local state operations
    @staticmethod
    def received(state: NState, msg: GiskardMessage) -> bool:
        """ Returns True if the msg is in the in_messages buffer """
        return state.in_messages.__contains__(msg)

    @staticmethod
    def sent(state: NState, msg: GiskardMessage) -> bool:
        """ Returns True if the msg is in the out_messages buffer """
        return state.out_messages.__contains__(msg)

    @staticmethod
    def record(state: NState, msg: GiskardMessage) -> NState:
        """ Adds a message to the out_message buffer """
        state_prime = copy.deepcopy(state)
        state_prime.out_messages.append(msg)
        return state_prime

    @staticmethod
    def record_plural(state: NState, lm: List[GiskardMessage]) -> NState:
        """ Adds several messages to the out_message buffer """
        state_prime = copy.deepcopy(state)
        state_prime.out_messages.extend(lm)
        return state_prime

    @staticmethod
    def add(state: NState, msg: GiskardMessage) -> NState:
        """ Broadcast messages are stored in the in_messages buffer,
        awaiting processing """
        state_prime = copy.deepcopy(state)
        state_prime.in_messages.append(msg)
        return state_prime

    @staticmethod
    def add_plural(state: NState, lm: List[GiskardMessage]) -> NState:
        """ Adds several messages to the in_messages buffer """
        state_prime = copy.deepcopy(state)
        state_prime.in_messages.extend(lm)
        return state_prime

    @staticmethod
    def discard(state: NState, msg: GiskardMessage) -> NState:
        """ Removes an invalid message from the in_messages buffer """
        state_prime = copy.deepcopy(state)
        if msg in state_prime.in_messages:
            state_prime.in_messages.remove(msg)
        else:
            print("\nGiskard.discard: couldn't remove msg: " + msg.__str__() + " it is not in the in_messages buffer: "
                  + state.in_messages.__str__())
        return state_prime

    @staticmethod
    def process(state: NState, msg: GiskardMessage) -> NState:
        """ Takes a message, removes it from the in_messages buffer
         and moves it to the counting_messages buffer, as it has been processed. """
        state_prime = Giskard.discard(state, msg)
        state_prime.counting_messages.append(msg)
        return state_prime

    @staticmethod
    def increment_view(state: NState) -> NState:
        """ View change happened,
        increments the view number, resets in_messages buffer and timeout variable. """
        state_prime = copy.deepcopy(state)
        state_prime.node_view += 1
        state_prime.in_messages = []
        state_prime.timeout = False
        return state_prime

    @staticmethod
    def flip_timeout(state: NState) -> NState:
        """ Sets the timeout variable to True """
        state_prime = copy.deepcopy(state)
        state_prime.timeout = True
        return state_prime

    # endregion

    # region local state properties
    @staticmethod
    def view_valid(state: NState, msg: GiskardMessage) -> bool:
        """ A message has a valid view with respect to a local state when it is equal to the
        local state's current view. Nodes only process "non-expired" messages sent in its current view. """
        return state.node_view == msg.view

    # endregion

    # region byzantine behaviour
    """ The primary form of Byzantine behavior Giskard considers is double voting:
    sending two PrepareVote messages for two different blocks of the same height within the same view.
    We call two messages equivocating if they evidence double voting behavior of their sender. """

    @staticmethod
    def equivocating_messages(state: NState, msg1: GiskardMessage, msg2: GiskardMessage) -> bool:
        return msg1 in state.out_messages \
            and msg2 in state.out_messages \
            and msg1.view == msg2.view \
            and msg1.message_type == GiskardMessage.CONSENSUS_GISKARD_PREPARE_VOTE \
            and msg2.message_type == GiskardMessage.CONSENSUS_GISKARD_PREPARE_VOTE \
            and msg1.block != msg2.block \
            and msg1.block.block_num == msg2.block.block_num

    @staticmethod
    def exists_same_height_block_old(state: NState, b: GiskardBlock) -> bool:
        """ Duplicate block checking for either:
        - a PrepareVote message in the processed message buffer for a different block of the same height, or
        - a PrepareVote or PrepareQC message in the sent message buffer for a different block of the same height. """
        msg: GiskardMessage
        for msg in state.counting_messages:
            if msg.message_type == GiskardMessage.CONSENSUS_GISKARD_PREPARE_BLOCK \
                    and b.block_num == msg.block_num \
                    and b != msg.block:
                return True
        for msg in state.out_messages:
            if (msg.message_type == GiskardMessage.CONSENSUS_GISKARD_PREPARE_VOTE \
                or msg.message_type == GiskardMessage.CONSENSUS_GISKARD_PREPARE_QC) \
                    and b.block_num == msg.block_num \
                    and b != msg.block:
                return True
        return False

    @staticmethod
    def exists_same_height_block(state: NState, b: GiskardBlock) -> bool:
        """ Returns True if there is a block in the out_messages buffer with the same height in the same view """
        msg: GiskardMessage
        for msg in state.out_messages:
            if msg.message_type == GiskardMessage.CONSENSUS_GISKARD_PREPARE_VOTE \
                    and b.block_num == msg.block.block_num \
                    and b != msg.block \
                    and msg.view == state.node_view:
                return True
        return False

    @staticmethod
    def exists_same_height_PrepareBlock(state: NState, b: GiskardBlock) -> bool:
        msg: GiskardMessage
        for msg in state.counting_messages:
            if msg.message_type == GiskardMessage.CONSENSUS_GISKARD_PREPARE_BLOCK \
                    and b.block_num == msg.block.block_num \
                    and b != msg.block\
                    and msg.view == state.node_view:
                return True
        return False

    @staticmethod
    def same_height_block_msg(state: NState, b: GiskardBlock, msg: GiskardMessage) -> bool:
        return msg.message_type == GiskardMessage.CONSENSUS_GISKARD_PREPARE_VOTE \
            and b.block_num == msg.block.block_num \
            and b != msg.block \
            and msg.view == state.node_view

    @staticmethod
    def handled_block_same_height_in_msgs(b, state: NState):
        for msg in state.counting_messages + state.out_messages:
            try:
                if b.block_num == msg.block.block_num \
                        and msg.view == state.node_view:
                    return True
            except AttributeError as e:
                print(msg.__str__())
                traceback.print_exc()
                print(e)
        return False

    @staticmethod
    def prepare_vote_already_sent(state: NState, b: GiskardBlock) -> bool:
        msg: GiskardMessage
        for msg in state.out_messages:
            if msg.message_type == GiskardMessage.CONSENSUS_GISKARD_PREPARE_VOTE \
                    and msg.block == b \
                    and msg.view == state.node_view:
                return True
        return False

    @staticmethod
    def prepare_qc_already_sent(state: NState, b: GiskardBlock) -> bool:
        msg: GiskardMessage
        for msg in state.out_messages:
            if msg.message_type == GiskardMessage.CONSENSUS_GISKARD_PREPARE_QC \
                    and msg.block == b \
                    and msg.view == state.node_view:
                return True
        return False

    @staticmethod
    def remove_higher_block_msgs_after_viewchange_qc(state: NState, block: GiskardBlock) -> NState:
        """ On ViewChangeQC, remove all msgs related to higher blocks than the one from ViewChangeQC
        This is to allow voting on what would be same height blocks """
        state_prime = copy.deepcopy(state)
        state_prime.counting_messages[:] = [msg for msg in state_prime.counting_messages
                                            if msg.block.block_num <= block.block_num]
        state_prime.out_messages[:] = [msg for msg in state_prime.out_messages
                                       if msg.block.block_num <= block.block_num]
        return state_prime

    # endregion

    # region prepare stage definitions
    """  Blocks in Giskard go through three stages: Prepare, Precommit and Commit.
    The local definitions of these three stages are: 
    - a block is in prepare stage in some local state s iff it has received quorum PrepareVote messages
      or a PrepareQC message in the current view or some previous view, and
    - a block is in precommit stage in some local state s iff its child block is in prepare stage in s, and
    - a block is in commit stage in some local state s iff its child block is in precommit stage in s.
    
    We can parameterize the definition of a block being in prepare stage by some view. """

    @staticmethod
    def processed_PrepareVote_in_view_about_block(state: NState, view: int, b: GiskardBlock) -> List[GiskardMessage]:
        """ Processed PrepareVote messages in some view about some block: """
        return list(filter(lambda msg: msg.message_type == GiskardMessage.CONSENSUS_GISKARD_PREPARE_VOTE
                                       and msg.block == b
                                       and msg.view == view, state.counting_messages + state.out_messages))

    @staticmethod
    def vote_quorum_in_view(state: NState, view: int, b: GiskardBlock, peers) -> bool:
        """ Returns True if there is a vote quorum in the given view, for the given block """
        if b is None:
            print("vote_quorum_in_view: block given was None")
            return False
        if GiskardGenesisBlock() == b and view == 0:  # TODO check genesis block behaviour and make it consistent
            return True
        return Giskard.quorum(Giskard.processed_PrepareVote_in_view_about_block(state, view, b), peers)

    @staticmethod
    def get_vote_quorum_msg_in_view(state: NState, view: int, b: GiskardBlock, peers) -> GiskardMessage:
        """ Returns True if there is a vote quorum in the given view, for the given block """
        if Giskard.quorum(Giskard.processed_PrepareVote_in_view_about_block(state, view, b), peers):
            return Giskard.processed_PrepareVote_in_view_about_block(state, view, b)[-1]

    @staticmethod
    def PrepareQC_in_view(state: NState, view: int, b: GiskardBlock) -> bool:
        """ Returns True if there is a PrepareQC message in the counting_blocks buffer,
        for the given block and view """
        if b is None:
            print("PrepareQC_in_view: block given was None")
            return False
        for msg in state.counting_messages:
            if msg.view == view \
                    and msg.block == b \
                    and msg.message_type == GiskardMessage.CONSENSUS_GISKARD_PREPARE_QC:
                return True
        return False

    @staticmethod
    def get_PrepareQC_in_view(state: NState, view: int, b: GiskardBlock) -> GiskardMessage:
        """ Returns True if there is a PrepareQC message in the counting_blocks buffer,
        for the given block and view """
        for msg in state.counting_messages:
            if msg.view == view \
                    and msg.block == b \
                    and msg.message_type == GiskardMessage.CONSENSUS_GISKARD_PREPARE_QC:
                return msg
        return None

    @staticmethod
    def prepare_stage_in_view(state: NState, view: int, b: GiskardBlock, peers) -> bool:
        """ Returns True if there is a vote quorum or a PrepareQC message for the given block and view """
        return Giskard.vote_quorum_in_view(state, view, b, peers) or Giskard.PrepareQC_in_view(state, view, b)

    @staticmethod
    def prepare_stage(state: NState, b: GiskardBlock, peers) -> bool:
        """ We use this parameterized definition to define the general version of a block being
        in prepare stage: A block has reached prepare stage in some state if it has reached prepare
        stage in some view that is less than or equal to the current view. """
        if b is None:
            print("prepare_stage: block given was None")
            return False
        if GiskardGenesisBlock() == b:
            return True
        v_prime = state.node_view
        while v_prime >= 0:
            if Giskard.prepare_stage_in_view(state, v_prime, b, peers):
                return True
            v_prime -= 1
        return False

    @staticmethod
    def get_quorum_msg_in_view(state: NState, view: int, b: GiskardBlock, peers) -> GiskardMessage:
        """ Returns the PrepareQC or vote-quorum msg for the given block,
         if there is one in the given view """
        if Giskard.PrepareQC_in_view(state, view, b):
            return Giskard.get_PrepareQC_in_view(state, view, b)
        return Giskard.get_vote_quorum_msg_in_view(state, view, b, peers)

    @staticmethod
    def get_quorum_msg_for_block(state: NState, b: GiskardBlock, peers) -> GiskardMessage:
        """ Returns the PrepareQC or Vote-quorum msg for the given block,
         if there is one """
        if b is None:  # TODO check if this might lead to issues
            print("get_quorum_msg_for_block was given a None type block")
            return Giskard.GenesisBlock_message(state)
        if GiskardGenesisBlock() == b:
            return Giskard.GenesisBlock_message(state)
        v_prime = state.node_view
        while v_prime >= 0:
            if Giskard.prepare_stage_in_view(state, v_prime, b, peers):
                return Giskard.get_quorum_msg_in_view(state, v_prime, b, peers)
            v_prime -= 1
        return None

    # endregion

    # region view change definitions
    """           Participating nodes in Giskard vote in units of time called views. Each view has the same
    fixed set of participating nodes, which consists of one block proposer for the view, and
    validators. Participating nodes take turns to be the block proposer, and the identity of
    the block proposer for any given view is known to all participating nodes. A view generally
    proceeds as follows: at the beginning of the view, the block proposer proposes a fixed
    number of blocks, which validators vote on. If all blocks receive quorum votes, the nodes
    increment their local view and wait for new block proposals in the new view. Otherwise,
    a timeout occurs, and nodes exchange messages to communicate their acknowledgement of the
    timeout and determine the parent block for the first block of the new view. The end of a
    view for a participating node is marked by either: 
    - the last block proposed by the block proposer reaching prepare stage in its local state, or 
    - the receipt of a ViewChangeQC message following a timeout.
    
    We call the former a normal view change, and the latter an abnormal view change.
    Because we assume that all local clocks are synchronized, in the case of an abnormal
    view change, all nodes timeout at once. However, because nodes process messages at
    different speeds, blocks reach prepare stage at different speeds, and consequently,
    in the case of a normal view change, nodes are not guaranteed to increment their
    local view at the same time.
    
    In the case of an abnormal view change, the timeout flag is simultaneously flipped
    for all participating nodes, and each sends a ViewChange message containing their
    local highest block at prepare stage. Nodes then enter a liminal stage in which only
    three kinds of messages can be processed: PrepareQC, ViewChange and ViewChangeQC.
    
    Nodes will ignore all block proposals and block votes during this stage. Upon
    receiving quorum ViewChange messages, validator nodes increment their view and wait
    for new blocks to be proposed. The new block proposer aggregates the max height
    block from its received ViewChange messages, and uses it to produce new blocks
    for the new view.
    
    The following section contains definitions required for abnormal view changes. """

    @staticmethod
    def processed_ViewChange_in_view(state: NState, view: int) -> List[GiskardMessage]:
        """ Quorum ViewChange messages in some view """
        return list(filter(lambda msg: msg.message_type == GiskardMessage.CONSENSUS_GISKARD_VIEW_CHANGE
                                       and msg.view == view, state.counting_messages))

    @staticmethod
    def view_change_quorum_in_view(state: NState, view: int, peers) -> bool:
        """ Returns True if there is a quorum of ViewChange messages for the given view """
        processed_msgs = Giskard.processed_ViewChange_in_view(state, view)
        return Giskard.quorum(processed_msgs, peers)

    @staticmethod
    def highest_ViewChange_block_in_view(state: NState, view: int) -> GiskardBlock:  # TODO test this one
        """ Maximum height block from all processed ViewChange messages in view """
        return reduce(lambda x, y: x if x.block_num > y.block_num else y,
                      [msg.block for msg in Giskard.processed_ViewChange_in_view(state, view)],
                      GiskardGenesisBlock())

    @staticmethod
    def highest_ViewChange_block_height_in_view(state: NState, view: int) -> int:
        return Giskard.highest_ViewChange_block_in_view(state, view).block_num

    @staticmethod
    def last_block(b: GiskardBlock) -> bool:
        """ The last block of each view is identifiable to each participating node """
        return Giskard.b_last(b)

    @staticmethod
    def highest_prepare_block_in_view(state: NState, view: int, peers) -> GiskardBlock:  # TODO test this
        """ Because not every view is guaranteed to have a block in prepare stage,
        the definition of <<highest_prepare_block_in_view>> must be able to recursively
        search for the highest prepare block in all past views """
        if view == -1:
            return GiskardGenesisBlock()
        else:
            return reduce(lambda x, y: x if x.block_num > y.block_num else y,
                          [msg.block for msg in state.counting_messages + state.out_messages
                           if Giskard.prepare_stage_in_view(state, view, msg.block, peers)],
                          Giskard.highest_prepare_block_in_view(state, view - 1, peers))

    @staticmethod
    def test_reduce(view):
        lm = range(0, view)
        if view == -1:
            return 0
        else:
            return reduce(lambda x, y: x if x > y else y,
                          [valuelm for valuelm in lm
                           if (valuelm % 2) == 0],
                          Giskard.test_reduce(view - 1))

    @staticmethod
    def highest_prepare_block_message(state: NState, peers) -> GiskardMessage:
        """ The following definition constructs the ViewChange message to be
        sent by each participating node upon a timeout. """
        return GiskardMessage(GiskardMessage.CONSENSUS_GISKARD_PREPARE_QC,
                              state.node_view,
                              state.node_id,
                              Giskard.highest_prepare_block_in_view(state, state.node_view, peers),
                              GiskardGenesisBlock())

    # endregion

    # region message construction
    """      In Giskard, some message types "carry" other messages: 
    - PrepareBlock messages for the first block in a view carry the PrepareQC
      message of its parent block, PrepareBlock messages for non-first blocks
      carry the PrepareQC of the parent block of the first block, and
    - PrepareVote messages carry the PrepareQC message of its parent block, and
    - ViewChange messages carry the PrepareQC message of the highest local
      prepare stage block.
      
    We do not model this at the type level (i.e., by having inductive message
    type definitions), but rather simulate this behavior using pre- and post-conditions
    in our local transitions. For example, a node only processes a PrepareBlock message
    in the in message buffer if the PrepareQC message that is "piggybacked" onto has also
    been received, and the transition effectively processes both of these messages in one step.
    
    PrepareBlocks are computed from either: 
    - the final PrepareVote/PrepareQC message of the previous round, or 
    - the ViewChangeQC from the previous round. 
    
    The messages in both of these cases contain the parent block for the newly proposed blocks.
    Note that because blocks are proposed in sequence, only the first PrepareBlock message carries
    the parent block's PrepareQC message - the remaining blocks cannot do so because their parent
    blocks have not reached prepare stage yet, and therefore their PrepareQC messages cannot exist.
    Therefore, all PrepareBlock messages in a view carry the same PrepareQC message: that of
    the first block's parent. """

    @staticmethod
    def make_PrepareBlocks(state: NState, previous_msg: GiskardMessage, block_cache) -> List[GiskardMessage]:
        """ Note that although all PrepareBlock messages are produced and sent together in one
        single transition, this does not mean that:
        - they are processed at the same time, and
        - we falsely enforce the discipline that the second proposed block contains the first
          block's PrepareQC when in fact it has not reached PrepareQC. """
        """ # You don't always have 3 blocks at this point
        if len(block_cache.pending_blocks) < 3:
            messages = []
            block_index = block_cache.blocks_proposed_num
            while len(block_cache.pending_blocks) > 0 and block_index < 3:
                block_index += 1
                block = Giskard.generate_new_block(previous_msg.block, block_cache, block_index)
                messages.append(GiskardMessage(GiskardMessage.CONSENSUS_GISKARD_PREPARE_BLOCK,
                                               state.node_view,
                                               state.node_id,
                                               block,
                                               previous_msg.block))
            return messages """

        block1 = Giskard.generate_new_block(previous_msg.block, block_cache, 1, state.node_id)
        block2 = Giskard.generate_new_block(block1, block_cache, 2, state.node_id)
        block3 = Giskard.generate_last_block(block2, block_cache, state.node_id)  # labeled as last block in view
        block_cache.last_proposed_block = block3
        return [GiskardMessage(GiskardMessage.CONSENSUS_GISKARD_PREPARE_BLOCK,
                               state.node_view,
                               state.node_id,
                               block1,
                               previous_msg.block),  # PrepareQC of the highest block from previous round
                GiskardMessage(GiskardMessage.CONSENSUS_GISKARD_PREPARE_BLOCK,
                               state.node_view,
                               state.node_id,
                               block2,
                               previous_msg.block),  # PrepareQC of the highest block from previous round
                GiskardMessage(GiskardMessage.CONSENSUS_GISKARD_PREPARE_BLOCK,
                               state.node_view,
                               state.node_id,
                               block3,
                               previous_msg.block)]  # PrepareQC of the highest block from previous round

    @staticmethod
    def make_PrepareBlock(state: NState, previous_msg: GiskardMessage,
                          block_cache, block_index: int, block=None) -> GiskardMessage:
        if block is None:
            block = Giskard.generate_new_block(previous_msg.block, block_cache, block_index, state.node_id)
        return GiskardMessage(GiskardMessage.CONSENSUS_GISKARD_PREPARE_BLOCK,
                              state.node_view,
                              state.node_id,
                              block,
                              previous_msg.block)

    @staticmethod
    def make_PrepareVote(state: NState, quorum_msg: GiskardMessage,
                         prepareblock_msg: GiskardMessage) -> GiskardMessage:
        """ A <<PrepareVote>> carries the <<PrepareQC>> of its parent, and can only be sent
        after parent block reaches prepare stage, which means one of its inputs must be
        either a <<PrepareVote>> or <<PrepareQC>>.

        <<PrepareVote>>s are also computed from <<PrepareBlock>> messages,
        which means another one of its inputs must be a <<PrepareBlock>> message. """
        return GiskardMessage(GiskardMessage.CONSENSUS_GISKARD_PREPARE_VOTE,  # message type
                              state.node_view,  # view number
                              state.node_id,
                              prepareblock_msg.block,  # block to vote for
                              quorum_msg.block)

    @staticmethod
    def pending_PrepareVote(state: NState, quorum_msg: GiskardMessage, block_cache) -> List[GiskardMessage]:
        """ Nodes create <<PrepareVote>> messages upon receiving <<PrepareBlock>> messages
        for each block, and "wait" to send it until the parent block reaches prepare stage.
        This is modeled by constructing <<PrepareVote>> messages on-demand given that:
        - the parent block has just reached prepare stage, and
        - a <<PrepareBlock>> message exists for the child block.

        Constructing pending PrepareVote messages for child messages with existing PrepareBlocks. """
        """ CHANGE from the original specification
        here do I check for if a prepare vote has been already sent,
        this check is nowhere to be found in the coq code
        avoids sending too many messages, and expected from the english specification """
        """ CHANGE from the original specification
        lambda msg: msg.view == quorum_msg.view removed, needs to be prepare_block_msg instead,
        otherwise hinders voting for the first block of a new view """
        return list(map(lambda prepare_block_msg:
                        Giskard.make_PrepareVote(state, quorum_msg, prepare_block_msg),
                        filter(lambda msg: not Giskard.exists_same_height_block(state, msg.block)
                                           and Giskard.parent_ofb(msg.block, quorum_msg.block, block_cache)
                                           and msg.message_type == GiskardMessage.CONSENSUS_GISKARD_PREPARE_BLOCK
                                           and not Giskard.prepare_vote_already_sent(state, msg.block)
                                           and msg.view == state.node_view,
                               state.counting_messages)))

    @staticmethod
    def pending_PrepareVote_malicious(state: NState, quorum_msg: GiskardMessage, block_cache) -> List[GiskardMessage]:
        """ Extra function, as original specification would not maliciously vote for a block,
        as the original pending_PrepareVote checks for exists_same_height_block """
        """ CHANGE from the original specification
        here do I check for if a prepare vote has been already sent,
        this check is nowhere to be found in the coq code"""
        """ CHANGE from the original specification
        lambda msg: msg.view == quorum_msg.view removed, why is that there makes no sense,
        hinders voting for the first block of a new view"""
        return list(map(lambda prepare_block_msg:
                        Giskard.make_PrepareVote(state, quorum_msg, prepare_block_msg),
                        filter(lambda msg: Giskard.parent_ofb(msg.block, quorum_msg.block, block_cache)
                                           and msg.message_type == GiskardMessage.CONSENSUS_GISKARD_PREPARE_BLOCK
                                           and not Giskard.prepare_vote_already_sent(state, msg.block),
                               state.counting_messages)))

    @staticmethod
    def make_PrepareQC(state: NState, msg: GiskardMessage) -> GiskardMessage:
        """ PrepareQC messages carry nothing, and can only be sent after a quorum number of PrepareVotes,
        which means its only input is a PrepareVote containing the relevant block. """
        return GiskardMessage(GiskardMessage.CONSENSUS_GISKARD_PREPARE_QC,  # message type
                              state.node_view,  # view number
                              state.node_id,
                              msg.block,
                              GiskardGenesisBlock())  # TODO need block from last view here

    @staticmethod
    def make_ViewChange(state: NState, peers) -> GiskardMessage:
        """ ViewChange messages carry the <<PrepareQC>> message of the highest block to
        reach prepare stage, and since they are triggered by timeouts, no input
        message is required. """
        return GiskardMessage(GiskardMessage.CONSENSUS_GISKARD_VIEW_CHANGE,
                              state.node_view,
                              state.node_id,
                              Giskard.highest_prepare_block_in_view(state, state.node_view, peers),
                              GiskardGenesisBlock())  # TODO need block from last view here

    @staticmethod
    def make_ViewChangeQC(state: NState, highest_msg: GiskardMessage) -> GiskardMessage:
        """ Upon receiving quorum <<ViewChange>> messages, the block proposer for the new
        view aggregates the max height block from all the <<ViewChange>> messages and
        sends a <<ViewChangeQC>> containing this block, alongside a <<PrepareQC>> message
        evidencing its prepare stage. """
        return GiskardMessage(GiskardMessage.CONSENSUS_GISKARD_VIEW_CHANGE_QC,
                              state.node_view,
                              state.node_id,
                              highest_msg.block,
                              GiskardGenesisBlock())  # TODO need block from last view here

    # endregion

    # region local state transitions
    """ Nodes are responsible for processing messages and updating their local state;
    broadcasting outgoing messages is handled by the network.
    
    In the following section, Giskard local state transitions are organized
    according to the type of message being processed. """

    """ Message type-agnostic actions """

    """ Block proposal-related definitions """

    @staticmethod
    def GenesisBlock_message(
            state: NState) -> GiskardMessage:  # Todo check with sawtooth genesis / call when genesis has been received / when generated with giskard consensus (later)
        return GiskardMessage(GiskardMessage.CONSENSUS_GISKARD_VIEW_CHANGE_QC,
                              0,
                              state.node_id,
                              GiskardGenesisBlock(),
                              GiskardGenesisBlock())

    @staticmethod
    def adhoc_ParentBlock_msg(state: NState, parent_block, view):
        return GiskardMessage(GiskardMessage.CONSENSUS_GISKARD_PREPARE_BLOCK,
                              view,
                              state.node_id,
                              parent_block,
                              GiskardGenesisBlock())

    @staticmethod
    def adhoc_ParentBlockQC_msg(state: NState, parent_block):
        return GiskardMessage(GiskardMessage.CONSENSUS_GISKARD_PREPARE_QC,
                              state.node_view - 1,
                              parent_block.signer_id,
                              parent_block,
                              GiskardGenesisBlock())

    @staticmethod
    def propose_block_init(state: NState, msg: GiskardMessage,
                           state_prime: NState, lm: List[GiskardMessage], node, block_cache, peers) -> bool:
        """ Returns True when a node transitioned to proposing blocks """
        lm_tmp = Giskard.make_PrepareBlocks(state,
                                            Giskard.GenesisBlock_message(state),
                                            block_cache)
        return state_prime == \
            Giskard.record_plural(state, lm_tmp) \
            and lm == lm_tmp \
            and state == NState(None, 0, state.node_id) \
            and Giskard.honest_node(node) \
            and Giskard.is_block_proposer(node, state.node_view, peers) \
            and not state.timeout

    @staticmethod
    def propose_block_init_set(state: NState, msg: GiskardMessage, block_cache) -> [NState, List[GiskardMessage]]:
        """ Actually does the transition to propose blocks """
        """if len(block_cache.pending_blocks) < 3:
            lm = []
            state_prime = state
            while block_cache.blocks_proposed_num <= LAST_BLOCK_INDEX_IDENTIFIER \
                    and len(block_cache.pending_blocks) > 0:
                # if block_cache.pending_blocks[0] != GiskardGenesisBlock():
                # block_cache.blocks_proposed_num += 1  # do not increase the counter when the first block was the genesis block
                # TODO solve problem with only having one block at a time in pending_blocks
                if block_cache.last_proposed_block is None:
                    previous_msg = Giskard.GenesisBlock_message(state)
                else:
                    previous_msg = GiskardMessage(GiskardMessage.CONSENSUS_GISKARD_PREPARE_BLOCK,
                                                  state.node_view,
                                                  state.node_id,
                                                  block_cache.last_proposed_block,
                                                  Giskard.GenesisBlock_message(state))
                msg = Giskard.make_PrepareBlock(
                    state, previous_msg, block_cache, block_cache.blocks_proposed_num, None)
                block_cache.blocks_proposed_num += 1
                block_cache.last_proposed_block = msg.block
                lm.append(msg)
                state_prime = Giskard.record(state, msg)
            return [state_prime, lm]"""
        previous_msg = Giskard.GenesisBlock_message(state)
        # msg = Giskard.make_PrepareBlock(
        #    state, previous_msg, block_cache, 0, None)
        lm = Giskard.make_PrepareBlocks(state, previous_msg, block_cache)
        state_prime = Giskard.record_plural(
            state, lm)
        block_cache.blocks_proposed_num = 3
        block_cache.last_proposed_block = lm[2].block
        return [state_prime, lm]

    @staticmethod
    def process_timeout(state: NState, msg: GiskardMessage,
                        state_prime: NState, lm: List[GiskardMessage], node, peers) -> bool:
        """  When the timeout happens, nodes enter a liminal phase where they are only allowed
        to process the following kinds of messages:
        - ViewChange from other nodes
        - ViewChangeQC
        - PrepareQC

        Upon timeout, nodes send a ViewChange message containing the highest block to reach
        Prepare stage in its current view, and the PrepareQC message attesting to that block's Prepare stage.

        It does not increment the view yet """
        return state_prime == \
            Giskard.record_plural(state, [Giskard.make_ViewChange(state,
                                                                  Giskard.highest_prepare_block_message(state, peers),
                                                                  peers)]) \
            and lm == [Giskard.make_ViewChange(state, peers), Giskard.highest_prepare_block_message(state, peers)] \
            and Giskard.honest_node(node) \
            and state.timeout

    @staticmethod
    def process_timeout_set(state: NState, msg: GiskardMessage, peers) -> [NState, List[GiskardMessage]]:
        lm = [Giskard.make_ViewChange(state, peers), Giskard.highest_prepare_block_message(state, peers)]
        state_prime = Giskard.record_plural(state, lm)
        return [state_prime, lm]

    @staticmethod
    def discard_view_invalid(state: NState, msg: GiskardMessage,
                             state_prime: NState, lm: List[GiskardMessage], node) -> bool:
        """ An expired message - discard and do not process. """
        return state_prime == Giskard.discard(state, msg) \
            and lm == [] \
            and Giskard.received(state, msg) \
            and Giskard.honest_node(node) \
            and not Giskard.view_valid(state, msg)

    @staticmethod
    def discard_view_invalid_set(state: NState, msg: GiskardMessage) -> [NState, List[GiskardMessage]]:
        state_prime = Giskard.discard(state, msg)
        return [state_prime, []]

    """ PrepareBlock message-related actions """

    # TODO use those two functions
    @staticmethod
    def process_PrepareBlock_duplicate(state: NState, msg: GiskardMessage,
                                       state_prime: NState, lm: List[GiskardMessage], node) -> bool:
        """ If a same height block has been seen - discard the message. """
        return state_prime == Giskard.discard(state, msg) \
            and lm == [] \
            and Giskard.received(state, msg) \
            and Giskard.honest_node(node) \
            and msg.message_type == GiskardMessage.CONSENSUS_GISKARD_PREPARE_BLOCK \
            and Giskard.view_valid(state, msg) \
            and not state.timeout \
            and Giskard.exists_same_height_PrepareBlock(state, msg.block)

    @staticmethod
    def process_PrepareBlock_duplicate_set(state: NState, msg: GiskardMessage) -> [NState, List[GiskardMessage]]:
        state_prime = Giskard.discard(state, msg)
        return [state_prime, []]

    @staticmethod
    def process_PrepareBlock_pending_vote(state: NState, msg: GiskardMessage,
                                          state_prime: NState, lm: List[GiskardMessage], node, block_cache,
                                          peers) -> bool:
        """ Parent block has not reached Prepare - "add its PrepareVote to pending buffer" by simply
        processing the PrepareBlock message and waiting for parent block to reach quorum. """
        return state_prime == Giskard.process(state, msg) \
            and lm == [] \
            and Giskard.received(state, msg) \
            and Giskard.honest_node(node) \
            and msg.message_type == GiskardMessage.CONSENSUS_GISKARD_PREPARE_BLOCK \
            and Giskard.view_valid(state, msg) \
            and not state.timeout \
            and not Giskard.exists_same_height_PrepareBlock(state, msg.block) \
            and not Giskard.prepare_stage(state, Giskard.parent_of(msg.block, block_cache), peers)

    @staticmethod
    def process_PrepareBlock_pending_vote_set(state: NState, msg: GiskardMessage) -> [NState, List[GiskardMessage]]:
        state_prime = Giskard.process(state, msg)
        return [state_prime, []]

    @staticmethod
    def process_PrepareBlock_vote(state: NState, msg: GiskardMessage,
                                  state_prime: NState, lm: List[GiskardMessage], node, block_cache, peers) -> bool:
        """ Parent block has reached QC - send PrepareVote for the block in that message and record in out buffer """
        """ CHANGE from the original specification
        PrepareBlockmsg is passed in, but we also need the quorum msg """
        """ CHANGE from the original specification
        pending_PrepareVote requires the msg to be in the counting msg buffer """
        quorum_msg = Giskard.adhoc_ParentBlockQC_msg(state, GiskardGenesisBlock())
        if GiskardGenesisBlock() == msg.block:
            parent_block = GiskardGenesisBlock()
        else:
            parent_block = block_cache.block_store.get_parent_block(msg.block)
        if parent_block is not None and Giskard.prepare_stage(state, parent_block, peers):
            quorum_msg = Giskard.get_quorum_msg_for_block(state, parent_block, peers)
        elif msg.block.block_num - 1 == msg.piggyback_block.block_num \
                and msg.block.previous_id == msg.piggyback_block.block_id:
            parent_block = msg.piggyback_block
            quorum_msg = Giskard.adhoc_ParentBlockQC_msg(state, parent_block)
        lm_prime = Giskard.pending_PrepareVote(Giskard.process(state, msg), quorum_msg, block_cache)
        return state_prime == \
            Giskard.record_plural(Giskard.process(state, msg), lm_prime) \
            and lm == lm_prime \
            and Giskard.received(state, msg) \
            and Giskard.honest_node(node) \
            and msg.message_type == GiskardMessage.CONSENSUS_GISKARD_PREPARE_BLOCK \
            and Giskard.view_valid(state, msg) \
            and not state.timeout \
            and Giskard.prepare_stage(state, parent_block, peers)

    @staticmethod
    def process_PrepareBlock_vote_set(state: NState, msg: GiskardMessage,
                                      block_cache, peers) -> [NState, List[GiskardMessage]]:
        """ CHANGE from the original specification
        PrepareBlockmsg is passed in, but we also need the quorum msg """
        """ CHANGE from the original specification
        pending_PrepareVote requires the msg to be in the counting msg buffer """
        quorum_msg = Giskard.adhoc_ParentBlockQC_msg(state, GiskardGenesisBlock())
        if GiskardGenesisBlock() == msg.block:
            parent_block = GiskardGenesisBlock()
        else:
            parent_block = block_cache.block_store.get_parent_block(msg.block)
        if parent_block is not None and Giskard.prepare_stage(state, parent_block, peers):
            quorum_msg = Giskard.get_quorum_msg_for_block(state, parent_block, peers)
        elif msg.block.block_num - 1 == msg.piggyback_block.block_num \
                and msg.block.previous_id == msg.piggyback_block.block_id:
            parent_block = msg.piggyback_block
            quorum_msg = Giskard.adhoc_ParentBlockQC_msg(state, parent_block)
        lm = Giskard.pending_PrepareVote(Giskard.process(state, msg), quorum_msg, block_cache)
        state_prime = Giskard.record_plural(Giskard.process(state, msg), lm)
        return [state_prime, lm]

    """ PrepareVote message-related actions """

    @staticmethod
    def process_PrepareVote_wait(state: NState, msg: GiskardMessage,
                                 state_prime: NState, lm: List[GiskardMessage], node, peers) -> bool:
        """ Block has not reached prepare stage - wait to send PrepareVote messages for child """
        return state_prime == Giskard.process(state, msg) \
            and lm == [] \
            and Giskard.received(state, msg) \
            and Giskard.honest_node(node) \
            and msg.message_type == GiskardMessage.CONSENSUS_GISKARD_PREPARE_VOTE \
            and Giskard.view_valid(state, msg) \
            and not state.timeout \
            and not Giskard.prepare_stage_in_view(Giskard.process(state, msg), state.node_view, msg.block, peers)

    @staticmethod
    def process_PrepareVote_wait_set(state: NState, msg: GiskardMessage) -> [NState, List[GiskardMessage]]:
        state_prime = Giskard.process(state, msg)
        return [state_prime, []]

    @staticmethod
    def process_PrepareVote_vote(state: NState, msg: GiskardMessage,
                                 state_prime: NState, lm: List[GiskardMessage], node, block_cache, peers) -> bool:
        """ Block is about to reach QC - send PrepareVote messages for child block if it exists and send PrepareQC
        vote_quorum means quorum PrepareVote messages """
        """ CHANGE from the original specification
        it only makes sense here to check if there is a same height block
        for the prepareVote msgs that we will be sending out """
        """ CHANGE from the original specification
        checking for prepareQC already sent, to avoid too many messages """
        # TODO is checking in the out_messages enough with exists_same_height_block ?
        lm_prime = []
        if not Giskard.prepare_qc_already_sent(state, msg.block):
            lm_prime.append(Giskard.make_PrepareQC(state, msg))
        lm_prime = lm_prime + Giskard.pending_PrepareVote(state, msg, block_cache)
        for m in lm_prime:
            if m.message_type == GiskardMessage.CONSENSUS_GISKARD_PREPARE_VOTE:
                if Giskard.exists_same_height_block(state, m.block):
                    return False
        return state_prime == \
            Giskard.process(Giskard.record_plural(
                state, lm_prime),
                msg) \
            and lm == lm_prime \
            and Giskard.received(state, msg) \
            and Giskard.honest_node(node) \
            and msg.message_type == GiskardMessage.CONSENSUS_GISKARD_PREPARE_VOTE \
            and Giskard.view_valid(state, msg) \
            and not state.timeout \
            and Giskard.vote_quorum_in_view(Giskard.process(state, msg), msg.view, msg.block, peers)

    @staticmethod
    def process_PrepareVote_vote_set(state: NState, msg: GiskardMessage, block_cache) -> [NState, List[GiskardMessage]]:
        """ CHANGE from the original specification
        checking for prepareQC already sent, to avoid too many messages """
        lm = []
        if not Giskard.prepare_qc_already_sent(state, msg.block):
            lm.append(Giskard.make_PrepareQC(state, msg))
        lm = lm + Giskard.pending_PrepareVote(state, msg, block_cache)
        state_prime = Giskard.process(Giskard.record_plural(
            state, lm), msg)
        return [state_prime, lm]

    """ PrepareQC message-related actions """

    """ PrepareQC messages are considered equivalent to a quorum of PrepareVote messages. 
    PrepareQC messages can be processed after timeout, so we do not require that timeout
    has not occurred.
    
    The PrepareQC message suffices to directly quorum a block, even if it has not received
    enough PrepareVote messages.
    
    Last block in view for to-be block proposer undergoing normal view change process:
    - increment view, and
    - propose block at height <<(S n)>> """

    @staticmethod
    def process_PrepareQC_last_block_new_proposer(state: NState, msg: GiskardMessage,
                                                  state_prime: NState, lm: List[GiskardMessage],
                                                  node, block_cache, peers) -> bool:
        """ Increment the view, propose next block """
        """ CHANGE from the original specification
        removed Giskard.last_block(msg.block) 
        as the last prepareqc message i can receive can be for a previous block """
        """ CHANGE from the original specification
        here we also need a quorum msg, 
        as the last received prepareQC doesn't need to be the one of the highest block """
        quorum_block = None
        for block in block_cache.blocks_reached_qc_current_view:
            if block.block_index == LAST_BLOCK_INDEX_IDENTIFIER:
                quorum_block = block
        quorum_msg = Giskard.get_quorum_msg_for_block(state, quorum_block, peers)
        lm_prime = Giskard.make_PrepareBlocks(
            Giskard.increment_view(Giskard.process(state, msg)),
            quorum_msg,
            block_cache)
        return state_prime == \
            Giskard.record_plural(Giskard.increment_view(Giskard.process(state, msg)),
                                  lm_prime) \
            and lm == lm_prime \
            and Giskard.received(state, msg) \
            and Giskard.honest_node(node) \
            and msg.message_type == GiskardMessage.CONSENSUS_GISKARD_PREPARE_QC \
            and Giskard.view_valid(state, msg) \
            and len(block_cache.blocks_reached_qc_current_view) == LAST_BLOCK_INDEX_IDENTIFIER \
            and Giskard.is_block_proposer(node, state.node_view + 1, peers)

    @staticmethod
    def process_PrepareQC_last_block_new_proposer_set(state: NState, msg: GiskardMessage,
                                                      block_cache, peers) -> [NState, List[GiskardMessage]]:
        """ CHANGE from the original specification
        here we also need a quorum msg,
        as the last received prepareQC doesn't need to be the one of the highest block """
        quorum_block = None
        for block in block_cache.blocks_reached_qc_current_view:
            if block.block_index == LAST_BLOCK_INDEX_IDENTIFIER:
                quorum_block = block
        quorum_msg = Giskard.get_quorum_msg_for_block(state, quorum_block, peers)
        lm = Giskard.make_PrepareBlocks(Giskard.increment_view(Giskard.process(state, msg)), quorum_msg, block_cache)
        state_prime = Giskard.record_plural(Giskard.increment_view(Giskard.process(state, msg)),
                                            lm)
        return [state_prime, lm]

    @staticmethod
    def process_PrepareQC_last_block(state: NState, msg: GiskardMessage,
                                     state_prime: NState, lm: List[GiskardMessage], node, block_cache, peers) -> bool:
        """ Last block in the view for to-be validator - increment view
        No child blocks can exist, so we don't need to send anything """
        """ CHANGE from the original specification
        removed Giskard.last_block(msg.block) 
        as the last prepareqc message i can receive can be for a previous block"""
        return state_prime == Giskard.increment_view(Giskard.process(state, msg)) \
            and lm == [] \
            and Giskard.received(state, msg) \
            and Giskard.honest_node(node) \
            and msg.message_type == GiskardMessage.CONSENSUS_GISKARD_PREPARE_QC \
            and Giskard.view_valid(state, msg) \
            and len(block_cache.blocks_reached_qc_current_view) == LAST_BLOCK_INDEX_IDENTIFIER \
            and not Giskard.is_block_proposer(node, state.node_view + 1, peers)

    @staticmethod
    def process_PrepareQC_last_block_set(state: NState, msg: GiskardMessage) -> [NState, List[GiskardMessage]]:
        state_prime = Giskard.increment_view(Giskard.process(state, msg))
        return [state_prime, []]

    @staticmethod
    def process_PrepareQC_non_last_block(state: NState, msg: GiskardMessage,
                                         state_prime: NState, lm: List[GiskardMessage], node, block_cache) -> bool:
        """ Not-the-last block in the view - send PrepareVote messages for child block and wait """
        """ CHANGE from the original specification
        removed not state.timeout, as we can process PrepareQC msgs during timeouts"""
        lm_prime = Giskard.pending_PrepareVote(state, msg, block_cache)
        return state_prime == Giskard.process(
            Giskard.record_plural(state, lm_prime),
            msg) \
            and lm == lm_prime \
            and Giskard.received(state, msg) \
            and Giskard.honest_node(node) \
            and msg.message_type == GiskardMessage.CONSENSUS_GISKARD_PREPARE_QC \
            and Giskard.view_valid(state, msg) \
            and not len(block_cache.blocks_reached_qc_current_view) == LAST_BLOCK_INDEX_IDENTIFIER

    @staticmethod
    def process_PrepareQC_non_last_block_set(state: NState, msg: GiskardMessage, block_cache) -> [NState,
                                                                                                  List[GiskardMessage]]:
        lm = Giskard.pending_PrepareVote(state, msg, block_cache)
        state_prime = Giskard.process(
            Giskard.record_plural(state, lm), msg)
        return [state_prime, lm]

    """ ViewChange message-related actions """

    """ ViewChange messages can be processed after timeout, so we do not require that timeout has not occurred.
    
    Process ViewChange at quorum for to-be block proposer:
    - send highest PrepareQC message,
    - send ViewChangeQC message,
    - increment view, and
    - propose new block according to highest block in all quorum ViewChange messages. """

    @staticmethod
    def higher_message(msg1: GiskardMessage, msg2: GiskardMessage) -> GiskardMessage:
        if msg1.block == Giskard.higher_block(msg1.block, msg2.block):
            return msg1
        else:
            return msg2

    @staticmethod
    def highest_message_in_list(node, message_type, lm: List[GiskardMessage]) -> GiskardMessage:
        return functools.reduce(lambda x, y: x if x.block.block_num > y.block.block_num else y,
                                lm, GiskardMessage(message_type, 0, node, GiskardGenesisBlock(), GiskardGenesisBlock()))

    @staticmethod
    def highest_ViewChange_message(state: NState) -> GiskardMessage:
        return Giskard.highest_message_in_list(state.node_id,
                                               GiskardMessage.CONSENSUS_GISKARD_VIEW_CHANGE,
                                               Giskard.processed_ViewChange_in_view(state, state.node_view))

    @staticmethod
    def get_view_highest_ViewChange_message_node_view(state: NState, msg: GiskardMessage) -> bool:
        if msg.message_type == GiskardMessage.CONSENSUS_GISKARD_VIEW_CHANGE:
            return msg.view == state.node_view \
                and msg in state.counting_messages \
                and Giskard.highest_ViewChange_message(state).view == state.node_view

    @staticmethod
    def get_view_highest_ViewChange_message_in_counting_messages(state: NState, msg: GiskardMessage) -> bool:
        if msg.message_type == GiskardMessage.CONSENSUS_GISKARD_VIEW_CHANGE:
            return msg.view == state.node_view \
                and msg in state.counting_messages \
                and Giskard.highest_ViewChange_message(state) in state.counting_messages

    @staticmethod
    def process_ViewChange_quorum_new_proposer(state: NState, msg: GiskardMessage,
                                               state_prime: NState, lm: List[GiskardMessage],
                                               node, block_cache, peers) -> bool:
        """ For better understandability look at process_ViewChange_quorum_new_proposer_set """
        """ CHANGE from the original specification TODO insert back in but with getting the actual msg
        this might work, but couldn't find a way to do it properly yet
        and Giskard.received(state, GiskardMessage(GiskardMessage.CONSENSUS_GISKARD_PREPARE_QC,
                                                   # This condition is necessary given ViewChange sending behavior
                                                   msg.view,
                                                   msg.sender,
                                                   Giskard.highest_ViewChange_message(
                                                       Giskard.process(state, msg)).block,
                                                   GiskardGenesisBlock())) """
        # The input has to include the current ViewChange message, just in
        # case that is the one which contains the highest block
        msg_qc = GiskardMessage(GiskardMessage.CONSENSUS_GISKARD_PREPARE_QC,  # PrepareQC of the highest block
                                state.node_view,
                                state.node_id,
                                Giskard.highest_ViewChange_message(Giskard.process(state, msg)).block,
                                GiskardGenesisBlock())
        msg_pr = GiskardMessage(GiskardMessage.CONSENSUS_GISKARD_PREPARE_QC,
                                # Send ViewChangeQC message before incrementing view to ensure the others can process it
                                msg.view,
                                msg.sender,
                                msg_qc.block,
                                GiskardGenesisBlock())
        lm_prime = [msg_qc,
                    # ViewChangeQC containing the highest block
                    Giskard.make_ViewChangeQC(state,
                                              Giskard.highest_ViewChange_message(Giskard.process(state, msg)))] \
                   + Giskard.make_PrepareBlocks(Giskard.increment_view(state),
                                                Giskard.highest_ViewChange_message(Giskard.process(state, msg)),
                                                block_cache)
        # Record new blocks after incrementing view
        return state_prime == \
            Giskard.record_plural(
                Giskard.increment_view(
                    Giskard.process(
                        Giskard.add(
                            Giskard.process(state, msg),
                            msg_pr),
                        msg_pr)),
                lm_prime) \
            and lm == lm_prime \
            and Giskard.received(state, msg) \
            and Giskard.honest_node(node) \
            and msg.message_type == GiskardMessage.CONSENSUS_GISKARD_VIEW_CHANGE \
            and Giskard.view_valid(state, msg) \
            and Giskard.view_change_quorum_in_view(Giskard.process(state, msg),
                                                   # ViewChange messages can be processed during timeout.It is important that the parameter here is (process state msg) and not simply state
                                                   state.node_view,
                                                   peers) \
            and Giskard.is_block_proposer(node, state.node_view + 1, peers)

    @staticmethod
    def process_ViewChange_quorum_new_proposer_set(state: NState,
                                                   msg: GiskardMessage, block_cache) -> [NState, List[GiskardMessage]]:
        #state = Giskard.remove_higher_block_msgs_after_viewchange_qc(state, msg.block)
        state_prime = Giskard.process(state, msg)
        msg_vc = Giskard.highest_ViewChange_message(state_prime)
        # TODO prepareqc is discarded, as node is not the proposer of this view,
        #  will handle this issue in the future as solving it might result in more unwanted behavior
        #  needing the last PrepareBlock message for that to get the correct proposer,
        #  but also need the other nodes to relax the discard msg, during viewchange process
        lm = [GiskardMessage(GiskardMessage.CONSENSUS_GISKARD_PREPARE_QC,
                             # Send ViewChangeQC message before incrementing view to ensure the others can process it
                             state.node_view,
                             state.node_id,
                             msg_vc.block,
                             GiskardGenesisBlock()),
              Giskard.make_ViewChangeQC(state, msg_vc)] \
             + Giskard.make_PrepareBlocks(Giskard.increment_view(state),
                                          msg_vc, block_cache)
        msg_pr = GiskardMessage(GiskardMessage.CONSENSUS_GISKARD_PREPARE_QC,
                                # Send ViewChangeQC message before incrementing view to ensure the others can process it
                                msg.view,
                                msg.sender,
                                msg_vc.block,
                                GiskardGenesisBlock())
        state_prime.counting_messages.append(msg_pr)
        state_prime.out_messages = state_prime.out_messages + lm
        state_prime_prime = NState(None,
                                   state_prime.node_view + 1,
                                   state_prime.node_id,
                                   [],
                                   state_prime.counting_messages,
                                   state_prime.out_messages,
                                   False)
        return [state_prime_prime, lm]

    @staticmethod
    def process_ViewChange_quorum_not_new_proposer(state: NState, msg: GiskardMessage,
                                                   state_prime: NState, lm: List[GiskardMessage],
                                                   node, peers) -> bool:
        """ Process ViewChange quorum reached,
        process and wait for receiving a ViewChangeQC msg from the next proposer """
        """ CHANGE from the original specification
        transition doesn't exist there, probably forgotten
        Needed for successful transition tests, as this transition happens """
        msg_vc = Giskard.highest_ViewChange_message(Giskard.process(state, msg))
        lm_prime = []
        if not Giskard.prepare_qc_already_sent(state, msg_vc.block):
            msg_pr = Giskard.make_PrepareQC(state, msg_vc)
            lm_prime.append(msg_pr)
        lm_prime.append(Giskard.make_ViewChangeQC(Giskard.process(state, msg), msg_vc))
        return state_prime == Giskard.record_plural(Giskard.process(state, msg), lm_prime) \
            and lm == lm_prime \
            and Giskard.received(state, msg) \
            and Giskard.honest_node(node) \
            and msg.message_type == GiskardMessage.CONSENSUS_GISKARD_VIEW_CHANGE \
            and Giskard.view_valid(state, msg) \
            and Giskard.view_change_quorum_in_view(
                Giskard.process(state, msg), state.node_view, peers) \
            and not Giskard.is_block_proposer(node, state.node_view + 1, peers)

    @staticmethod
    def process_ViewChange_quorum_not_new_proposer_set(state: NState,
                                                       msg: GiskardMessage) -> [NState, List[GiskardMessage]]:
        """ CHANGE from the original specification
        doesn't exist there, probably forgotten
        Needed for successful transition tests, as this transition happens """
        state_prime = Giskard.process(state, msg)
        msg_vc = Giskard.highest_ViewChange_message(state_prime)
        # TODO prepareqc is discarded, as node is not the proposer of this view,
        #  will handle this issue in the future as solving it might result in more unwanted behavior
        #  needing the last PrepareBlock message for that to get the correct proposer,
        #  but also need the other nodes to relax the discard msg, during viewchange process
        lm = []
        if not Giskard.prepare_qc_already_sent(state, msg_vc.block):
            msg_pr = Giskard.make_PrepareQC(state, msg_vc)
            lm.append(msg_pr)
        lm.append(Giskard.make_ViewChangeQC(state_prime, msg_vc))
        state_prime = Giskard.record_plural(state_prime, lm)
        return [state_prime, lm]

    @staticmethod
    def process_ViewChange_pre_quorum(state: NState, msg: GiskardMessage,
                                      state_prime: NState, lm: List[GiskardMessage],
                                      node, peers) -> bool:
        """ Process ViewChange before quorum - keep and wait for QC. """
        return state_prime == Giskard.process(state, msg) \
            and lm == [] \
            and Giskard.received(state, msg) \
            and Giskard.honest_node(node) \
            and msg.message_type == GiskardMessage.CONSENSUS_GISKARD_VIEW_CHANGE \
            and Giskard.view_valid(state, msg) \
            and not Giskard.view_change_quorum_in_view(
                Giskard.process(state, msg), state.node_view, peers)

    @staticmethod
    def process_ViewChange_pre_quorum_set(state: NState,
                                          msg: GiskardMessage) -> [NState, List[GiskardMessage]]:
        state_prime = Giskard.process(state, msg)
        return [state_prime, []]

    """ ViewChangeQC message-related actions """

    @staticmethod
    def process_ViewChangeQC_single(state: NState, msg: GiskardMessage,
                                    state_prime: NState, lm: List[GiskardMessage],
                                    node) -> bool:
        """ Process highest PrepareQC message, process ViewChangeQC, then increment view.
        Critically, this is where we enforce that the PrepareQC of the max height block
        is processed before view change occurs, otherwise nodes can get stuck during view change. """
        """ CHANGE from the original specification
        removed and Giskard.received(state, GiskardMessage(GiskardMessage.CONSENSUS_GISKARD_PREPARE_QC,
                                                       state.node_view,
                                                       msg.sender,
                                                       msg.block,
                                                       GiskardGenesisBlock())) \
        could be that not received yet, due to network problems -> out of order messages """
        return state_prime == \
            Giskard.increment_view(
                Giskard.process(state, msg)) \
            and lm == [] \
            and Giskard.received(state, msg) \
            and Giskard.honest_node(node) \
            and msg.message_type == GiskardMessage.CONSENSUS_GISKARD_VIEW_CHANGE_QC \
            and Giskard.view_valid(state, msg)

    @staticmethod
    def process_ViewChangeQC_single_set(state: NState,
                                        msg: GiskardMessage) -> [NState, List[GiskardMessage]]:
        state_prime = Giskard.increment_view(Giskard.process(state, msg))
        return [state_prime, []]

    """ Malicious node actions """

    # TODO use malicious_ignore
    @staticmethod
    def malicious_ignore(state: NState, msg: GiskardMessage,
                         state_prime: NState, lm: List[GiskardMessage],
                         node) -> bool:
        """ Malicious nodes can ignore messages """
        return state_prime == Giskard.discard(state, msg) \
            and lm == [] \
            and Giskard.received(state, msg) \
            and not Giskard.honest_node(node)

    @staticmethod
    def malicious_ignore_set(state: NState, msg: GiskardMessage) -> [NState, List[GiskardMessage]]:
        state_prime = Giskard.discard(state, msg)
        return [state_prime, []]

    @staticmethod
    def process_PrepareBlock_malicious_vote(state: NState, msg: GiskardMessage,
                                            state_prime: NState, lm: List[GiskardMessage],
                                            node, block_cache, peers) -> bool:
        """ Malicious nodes can double vote for two blocks of the same height """
        """ CHANGE from the original specification
        extra pending_PrepareVote function, as the honest one also checks for exists same height block """
        quorum_msg = Giskard.adhoc_ParentBlockQC_msg(state, GiskardGenesisBlock())
        if GiskardGenesisBlock() == msg.block:
            parent_block = GiskardGenesisBlock()
        else:
            parent_block = block_cache.block_store.get_parent_block(msg.block)
        if parent_block is not None and Giskard.prepare_stage(state, parent_block, peers):
            quorum_msg = Giskard.get_quorum_msg_for_block(state, parent_block, peers)
        elif msg.block.block_num - 1 == msg.piggyback_block.block_num \
                and msg.block.previous_id == msg.piggyback_block.block_id:
            parent_block = msg.piggyback_block
            quorum_msg = Giskard.adhoc_ParentBlockQC_msg(state, parent_block)
        lm_prime = Giskard.pending_PrepareVote_malicious(Giskard.process(state, msg), quorum_msg, block_cache)
        return state_prime == Giskard.record_plural(Giskard.process(state, msg),
                                                    lm_prime) \
            and lm == lm_prime \
            and Giskard.received(state, msg) \
            and not Giskard.honest_node(node) \
            and msg.message_type == GiskardMessage.CONSENSUS_GISKARD_PREPARE_BLOCK \
            and Giskard.view_valid(state, msg) \
            and Giskard.exists_same_height_block(state, msg.block)

    @staticmethod
    def process_PrepareBlock_malicious_vote_set(state: NState, msg: GiskardMessage,
                                                block_cache, peers) -> [NState, List[GiskardMessage]]:
        quorum_msg = Giskard.adhoc_ParentBlockQC_msg(state, GiskardGenesisBlock())
        if GiskardGenesisBlock() == msg.block:
            parent_block = GiskardGenesisBlock()
        else:
            parent_block = block_cache.block_store.get_parent_block(msg.block)
        if parent_block is not None and Giskard.prepare_stage(state, parent_block, peers):
            quorum_msg = Giskard.get_quorum_msg_for_block(state, parent_block, peers)
        elif msg.block.block_num - 1 == msg.piggyback_block.block_num \
                and msg.block.previous_id == msg.piggyback_block.block_id:
            parent_block = msg.piggyback_block
            quorum_msg = Giskard.adhoc_ParentBlockQC_msg(state, parent_block)
        lm = Giskard.pending_PrepareVote_malicious(Giskard.process(state, msg), quorum_msg, block_cache)
        state_prime = Giskard.record_plural(Giskard.process(state, msg), lm)
        return [state_prime, lm]

    """ Protocol transition type definitions """

    @staticmethod
    def get_transition(t, state: NState, msg: GiskardMessage,
                       state_prime: NState, lm: List[GiskardMessage],
                       node, block_cache, peers, old_version=False) -> bool:

        if t == giskard_state_transition_type.PROPOSE_BLOCK_INIT_TYPE:
            return Giskard.propose_block_init(state, msg, state_prime, lm, node, block_cache, peers)
        elif t == giskard_state_transition_type.DISCARD_VIEW_INVALID_TYPE:
            return Giskard.discard_view_invalid(state, msg, state_prime, lm, node)
        elif t == giskard_state_transition_type.PROCESS_PREPAREBLOCK_DUPLICATE_TYPE:
            return Giskard.process_PrepareBlock_duplicate(state, msg, state_prime, lm, node)
        elif t == giskard_state_transition_type.PROCESS_PREPAREBLOCK_PENDING_VOTE_TYPE:
            return Giskard.process_PrepareBlock_pending_vote(state, msg, state_prime, lm, node, block_cache, peers)
        elif t == giskard_state_transition_type.PROCESS_PREPAREBLOCK_VOTE_TYPE:
            if old_version:  # bug in the original Coq code, this is here for testing the impact
                return Giskard.process_PrepareBlock_pending_vote(state, msg, state_prime, lm, node, block_cache, peers)
            """ CHANGE from the original specification
            Fixed typo, here should be mapped to process_PrepareBlock_vote,
            and not to process_PrepareBlock_pending_vote again. """
            return Giskard.process_PrepareBlock_vote(state, msg, state_prime, lm, node, block_cache, peers)
        elif t == giskard_state_transition_type.PROCESS_PREPAREVOTE_VOTE_TYPE:
            return Giskard.process_PrepareVote_vote(state, msg, state_prime, lm, node, block_cache, peers)
        elif t == giskard_state_transition_type.PROCESS_PREPAREVOTE_WAIT_TYPE:
            return Giskard.process_PrepareVote_wait(state, msg, state_prime, lm, node, peers)
        elif t == giskard_state_transition_type.PROCESS_PREPAREQC_LAST_BLOCK_NEW_PROPOSER_TYPE:
            return Giskard.process_PrepareQC_last_block_new_proposer(state, msg, state_prime, lm, node, block_cache,
                                                                     peers)
        elif t == giskard_state_transition_type.PROCESS_PREPAREQC_LAST_BLOCK_TYPE:
            return Giskard.process_PrepareQC_last_block(state, msg, state_prime, lm, node, block_cache, peers)
        elif t == giskard_state_transition_type.PROCESS_PREPAREQC_NON_LAST_BLOCK_TYPE:
            return Giskard.process_PrepareQC_non_last_block(state, msg, state_prime, lm, node, block_cache)
        elif t == giskard_state_transition_type.PROCESS_VIEWCHANGE_QUORUM_NEW_PROPOSER_TYPE:
            return Giskard.process_ViewChange_quorum_new_proposer(state, msg, state_prime, lm, node, block_cache,
                                                                  peers)
        elif t == giskard_state_transition_type.PROCESS_VIEWCHANGE_QUORUM_NOT_NEW_PROPOSER_TYPE:
            return Giskard.process_ViewChange_quorum_not_new_proposer(state, msg, state_prime, lm, node, peers)
        elif t == giskard_state_transition_type.PROCESS_VIEWCHANGE_PRE_QUORUM_TYPE:
            return Giskard.process_ViewChange_pre_quorum(state, msg, state_prime, lm, node, peers)
        elif t == giskard_state_transition_type.PROCESS_VIEWCHANGEQC_SINGLE_TYPE:
            return Giskard.process_ViewChangeQC_single(state, msg, state_prime, lm, node)
        elif t == giskard_state_transition_type.PROCESS_PREPAREBLOCK_MALICIOUS_VOTE_TYPE:
            return Giskard.process_PrepareBlock_malicious_vote(state, msg, state_prime, lm, node, block_cache, peers)

    @staticmethod
    def create_mock_block_cache_from_counting_msgs(state: NState, state_prime: NState, peers) -> BlockCacheMock:
        blocks = []
        pending_blocks = []
        blocks_reached_qc_current_view = []
        for msg in state.counting_messages:
            if msg.block not in blocks:
                if Giskard.exists_same_height_block(state, msg.block):
                    msg_with_higher_view_exists = False
                    for m in state.counting_messages:
                        if m.view > msg.view:
                            msg_with_higher_view_exists = True
                    if msg_with_higher_view_exists:
                        continue
                blocks.append(msg.block)

        for msg in state.counting_messages + state.in_messages:
            if msg.block not in blocks_reached_qc_current_view and msg.view == state.node_view \
                    and msg.message_type == GiskardMessage.CONSENSUS_GISKARD_PREPARE_QC\
                    and msg.block.payload != "Beware, I am a malicious block":
                blocks_reached_qc_current_view.append(msg.block)

        if state_prime is not None:
            for msg in state_prime.out_messages:  # for the blocks that will be proposed during the transition
                if msg.block not in pending_blocks and msg.block not in blocks \
                        and msg.message_type == GiskardMessage.CONSENSUS_GISKARD_PREPARE_BLOCK:
                    pending_blocks.append(msg.block)
        blocks.sort(key=lambda b: b.block_num)
        pending_blocks.sort(key=lambda b: b.block_num)
        cache = BlockCacheMock([])
        cache.block_store.uncommitted_blocks = blocks
        cache.pending_blocks = pending_blocks
        cache.blocks_reached_qc_current_view = blocks_reached_qc_current_view
        return cache

    @staticmethod
    def get_inputs_for_transition(t, state: NState, state_prime: NState, peers, old_version=False):
        """ We only transmit the nstate to the tester, so we don't have other data, like the block_cache,
        so we need to generate those inputs from this and the next state.
        This proofs to be the spot for many bugs, as now the same executions need to be done in two places,
        the engine and here. Be careful when changing something in the engine, the transition functions or here"""
        msg = None
        block_cache = None
        lm = []
        dishonest = False
        if t == giskard_state_transition_type.PROPOSE_BLOCK_INIT_TYPE:
            block_cache = Giskard.create_mock_block_cache_from_counting_msgs(state, state_prime, peers)
            tmp_block_cache = copy.deepcopy(block_cache)
            lm = Giskard.propose_block_init_set(state, msg, tmp_block_cache)[1]
        elif t == giskard_state_transition_type.DISCARD_VIEW_INVALID_TYPE:
            msg = state.in_messages[0]  # Todo check in what order the in messages are processed
        elif t == giskard_state_transition_type.PROCESS_PREPAREBLOCK_DUPLICATE_TYPE:
            msg = state.in_messages[0]
        elif t == giskard_state_transition_type.PROCESS_PREPAREBLOCK_PENDING_VOTE_TYPE:
            msg = state.in_messages[0]
            block_cache = Giskard.create_mock_block_cache_from_counting_msgs(state, state_prime, peers)
        elif t == giskard_state_transition_type.PROCESS_PREPAREBLOCK_VOTE_TYPE:
            msg = state.in_messages[0]
            block_cache = Giskard.create_mock_block_cache_from_counting_msgs(state, state_prime, peers)
            if not old_version:  # bug in the original Coq code, this is here for testing the impact
                tmp_block_cache = copy.deepcopy(block_cache)
                quorum_msg = Giskard.adhoc_ParentBlockQC_msg(state, GiskardGenesisBlock())
                if GiskardGenesisBlock() == msg.block:
                    parent_block = GiskardGenesisBlock()
                else:
                    parent_block = tmp_block_cache.block_store.get_parent_block(msg.block)
                if parent_block is not None and Giskard.prepare_stage(state, parent_block, peers):
                    quorum_msg = Giskard.get_quorum_msg_for_block(state, parent_block, peers)
                elif msg.block.block_num - 1 == msg.piggyback_block.block_num \
                        and msg.block.previous_id == msg.piggyback_block.block_id:
                    parent_block = msg.piggyback_block
                    quorum_msg = Giskard.adhoc_ParentBlockQC_msg(state, parent_block)
                lm = Giskard.pending_PrepareVote(Giskard.process(state, msg), quorum_msg, tmp_block_cache)
        elif t == giskard_state_transition_type.PROCESS_PREPAREVOTE_VOTE_TYPE:
            msg = state.in_messages[0]
            block_cache = Giskard.create_mock_block_cache_from_counting_msgs(Giskard.process(state, msg), state_prime,
                                                                             peers)
            tmp_block_cache = copy.deepcopy(block_cache)
            lm = []
            if not Giskard.prepare_qc_already_sent(state, msg.block):
                lm.append(Giskard.make_PrepareQC(state, msg))
            lm = lm + Giskard.pending_PrepareVote(state, msg, tmp_block_cache)
        elif t == giskard_state_transition_type.PROCESS_PREPAREVOTE_WAIT_TYPE:
            msg = state.in_messages[0]
            block_cache = Giskard.create_mock_block_cache_from_counting_msgs(Giskard.process(state, msg), state_prime,
                                                                             peers)
        elif t == giskard_state_transition_type.PROCESS_PREPAREQC_LAST_BLOCK_NEW_PROPOSER_TYPE:
            msg = state.in_messages[0]
            block_cache = Giskard.create_mock_block_cache_from_counting_msgs(state, state_prime, peers)
            tmp_block_cache = copy.deepcopy(block_cache)
            lm = Giskard.make_PrepareBlocks(Giskard.increment_view(Giskard.process(state, msg)), msg, tmp_block_cache)
        elif t == giskard_state_transition_type.PROCESS_PREPAREQC_LAST_BLOCK_TYPE:
            msg = state.in_messages[0]
            block_cache = Giskard.create_mock_block_cache_from_counting_msgs(state, state_prime, peers)
        elif t == giskard_state_transition_type.PROCESS_PREPAREQC_NON_LAST_BLOCK_TYPE:
            msg = state.in_messages[0]
            block_cache = Giskard.create_mock_block_cache_from_counting_msgs(state, state_prime, peers)
            tmp_block_cache = copy.deepcopy(block_cache)
            lm = Giskard.pending_PrepareVote(state, msg, tmp_block_cache)
        elif t == giskard_state_transition_type.PROCESS_VIEWCHANGE_QUORUM_NEW_PROPOSER_TYPE:
            msg = state.in_messages[0]
            block_cache = Giskard.create_mock_block_cache_from_counting_msgs(state, state_prime, peers)
            tmp_block_cache = copy.deepcopy(block_cache)
            lm = [GiskardMessage(GiskardMessage.CONSENSUS_GISKARD_PREPARE_QC,
                                 # Send ViewChangeQC message
                                 # before incrementing view to ensure the others can process it
                                 state.node_view,
                                 state.node_id,
                                 Giskard.highest_ViewChange_message(Giskard.process(state, msg)).block,
                                 GiskardGenesisBlock()),
                  Giskard.make_ViewChangeQC(state,
                                            Giskard.highest_ViewChange_message(Giskard.process(state, msg)))] \
                 + Giskard.make_PrepareBlocks(Giskard.increment_view(state),
                                              Giskard.highest_ViewChange_message(Giskard.process(state, msg)),
                                              tmp_block_cache)  # send PrepareBlock messages
        elif t == giskard_state_transition_type.PROCESS_VIEWCHANGE_QUORUM_NOT_NEW_PROPOSER_TYPE:
            msg = state.in_messages[0]
            msg_vc = Giskard.highest_ViewChange_message(Giskard.process(state, msg))
            if not Giskard.prepare_qc_already_sent(Giskard.process(state, msg), msg_vc.block):
                msg_pr = Giskard.make_PrepareQC(Giskard.process(state, msg), msg_vc)
                lm.append(msg_pr)
            lm.append(Giskard.make_ViewChangeQC(Giskard.process(state, msg), msg_vc))
        elif t == giskard_state_transition_type.PROCESS_VIEWCHANGE_PRE_QUORUM_TYPE:
            msg = state.in_messages[0]
        elif t == giskard_state_transition_type.PROCESS_VIEWCHANGEQC_SINGLE_TYPE:
            msg = state.in_messages[0]
        elif t == giskard_state_transition_type.PROCESS_PREPAREBLOCK_MALICIOUS_VOTE_TYPE:
            msg = state.in_messages[0]
            block_cache = Giskard.create_mock_block_cache_from_counting_msgs(state, state_prime, peers)
            tmp_block_cache = copy.deepcopy(block_cache)
            quorum_msg = Giskard.adhoc_ParentBlockQC_msg(state, GiskardGenesisBlock())
            if GiskardGenesisBlock() == msg.block:
                parent_block = GiskardGenesisBlock()
            else:
                parent_block = tmp_block_cache.block_store.get_parent_block(msg.block)
            if parent_block is not None and Giskard.prepare_stage(state, parent_block, peers):
                quorum_msg = Giskard.get_quorum_msg_for_block(state, parent_block, peers)
            elif msg.block.block_num - 1 == msg.piggyback_block.block_num \
                    and msg.block.previous_id == msg.piggyback_block.block_id:
                parent_block = msg.piggyback_block
                quorum_msg = Giskard.adhoc_ParentBlockQC_msg(state, parent_block)
            lm = Giskard.pending_PrepareVote_malicious(Giskard.process(state, msg), quorum_msg,
                                                       tmp_block_cache)  # TODO check what i can do maliciously here
            dishonest = True
        return [msg, block_cache, lm, dishonest]

    # endregion

    # region global state
    """ In order to define protocol-following global state transitions,
    we first define an operation which does two things:
    - broadcasts outgoing messages from the transitioning node to the remaining nodes, and
    - records outgoing messages in the global message buffer.
    
    We adopt the convention that nodes do not send messages to themselves.
    This design choice is orthogonal to safety proofs because we do not
    reason about the number of participants required to reach a quorum. """

    @staticmethod
    def broadcast_messages(gstate: GState, state1: NState, state2: NState, lm: List[GiskardMessage], peers) -> GState:
        return GState(
            peers,
            {node: [state2] if node == state1.node_id else [Giskard.add_plural(gstate.gstate[node][-1], lm)]
            if node in peers else gstate.gstate[node][-1] for node in gstate.gstate},
            gstate.broadcast_msgs + lm)

    @staticmethod
    def GState_transition(gstate: GState, gstate_prime: GState, node, nodes, old_version=False):
        """ Protocol-following global state transitions are defined as a binary relation
        on pre-state and post-state, in which one participating node makes a protocol-following
        local state transition, and the network broadcasts and records its outgoing messages
        to the other participating nodes. """
        nstate = gstate.gstate[node][-1]
        nstate_prime = gstate_prime.gstate[node][-1]
        full_node = GiskardNode(node, False, None)
        transition_correct = False
        """ Go through all possible transitions,
        execute them on nstate and compare with nstate_prime (the next state) """
        for process in giskard_state_transition_type.all_transition_types:
            """ We only check this transition once, for the initial proposer,
            which won't have any messages in the out_messages at this point """
            if process == giskard_state_transition_type.PROPOSE_BLOCK_INIT_TYPE \
                    and len(nstate.out_messages) > 0:
                continue
            """ The in_message buffer is not allowed to be empty
            for any transition other than the initial block proposal"""
            if len(nstate.in_messages) == 0 \
                    and ((not Giskard.is_block_proposer(full_node, nstate.node_view, nodes))
                         or (Giskard.is_block_proposer(full_node, nstate.node_view, nodes)
                             and len(nstate.out_messages) > 0)):  # The state right after the initial proposal
                Giskard.inc_nr_of_transitions("/mnt/c/repos/sawtooth-giskard/tests/sawtooth_poet_tests/nr_of_irrelevant_transitions.json")
                return True
            """ Pull out inputs and create a mock block_cache """
            msg, block_cache, lm, dishonest = Giskard.get_inputs_for_transition(process, nstate, nstate_prime,
                                                                                nodes, old_version)
            full_node.block_cache = block_cache
            full_node.dishonest = dishonest
            """ Check if the transition the one given in the process variable """
            found_transition = Giskard.get_transition(process, nstate, msg, nstate_prime, lm, full_node,
                                                      block_cache, nodes, old_version)
            """ gstate_bmsgs = Giskard.broadcast_messages(gstate, nstate, nstate_prime, lm, nodes) 
            This is done in the spec, but complicates things, -> changed to what we basically want to proof """
            equal = True
            if equal:
                for m in lm:
                    if m not in gstate_prime.broadcast_msgs:
                        equal = False
                        break

            if found_transition and equal:
                transition_correct = True  # CHANGE FROM THE ORIGINAL SPECIFICATION there whole gstates are compared
                break
        if not transition_correct:  # check if transition was a timeout
            if len(nstate.in_messages) > 0 \
                    and nstate.in_messages[0].message_type == GiskardMessage.CONSENSUS_GISKARD_PREPARE_QC:
                hanging_prepareqc = nstate.in_messages.pop(0)  # hanging PrepareQC msg could still be in the in_buffer
            if nstate_prime != Giskard.flip_timeout(nstate):
                return False
            Giskard.inc_nr_of_transitions("/mnt/c/repos/sawtooth-giskard/tests/sawtooth_poet_tests/nr_of_correct_transitions.json")
            return nstate_prime == Giskard.flip_timeout(nstate)
        else:
            Giskard.inc_nr_of_transitions("/mnt/c/repos/sawtooth-giskard/tests/sawtooth_poet_tests/nr_of_correct_transitions.json")
            return True

    # endregion

    # region global state trace
    @staticmethod
    def protocol_trace(gtrace: GTrace, old_version=False) -> bool:
        nodes = list(set(gtrace.gtrace[-1].gstate.keys()))
        nodes.sort(key=lambda h: int(h, 16))
        Giskard.reset_nr_of_transitions("/mnt/c/repos/sawtooth-giskard/tests/sawtooth_poet_tests/nr_of_correct_transitions.json")
        Giskard.reset_nr_of_transitions("/mnt/c/repos/sawtooth-giskard/tests/sawtooth_poet_tests/nr_of_irrelevant_transitions.json")
        last_gstates = {}
        for node in nodes:
            last_gstates.update({node: None})
            for i in range(0, len(gtrace.gtrace)):
                if i > 0 and gtrace.gtrace[i].gstate[node][-1] == gtrace.gtrace[i - 1].gstate[node][-1]:
                    continue  # the gtrace is updated after every local state transition, so only one node will have a new transition -> skip the rest
                if i == len(gtrace.gtrace) - 1:
                    continue  # the last gstates have no further transitions so we skip them

                tmp_gstate = gtrace.gtrace[i]
                next_gstate = Giskard.get_nodes_next_state(gtrace, i, node)
                if not next_gstate or next_gstate.gstate[node][-1] == last_gstates[node]:  # this node has no further transition or it was already tested
                    continue
                if not Giskard.GState_transition(tmp_gstate, next_gstate, node, nodes, old_version):
                    Giskard.output_state_measures(len(nodes), len(gtrace.gtrace))
                    return False
                last_gstates.update({node: next_gstate.gstate[node][-1]})
        Giskard.output_state_measures(nodes, gtrace)
        return True

    @staticmethod
    def output_state_measures(nodes, gtrace):
        """ store quantitative measures """
        nr_of_nodes = len(nodes)
        node_states = Giskard.get_nr_of_states_per_node(gtrace, nodes)
        nr_of_transitions = 0
        for node in nodes:
            nr_of_transitions += node_states[node] - 1

        checked_transitions = Giskard.get_nr_of_transitions("/mnt/c/repos/sawtooth-giskard/tests/sawtooth_poet_tests/nr_of_correct_transitions.json")
        irrelevant_transitions = Giskard.get_nr_of_transitions("/mnt/c/repos/sawtooth-giskard/tests/sawtooth_poet_tests/nr_of_irrelevant_transitions.json")  # the transition from initial state to sth are not checked, as nothing happens during the transition, also the states that are left open, as the network got stopped before

        table = [['Total Transitions', 'Irrelevant Transitions', 'Tested Transitions', 'Untested Transitions'],
                 [nr_of_transitions, irrelevant_transitions, checked_transitions,
                  nr_of_transitions - irrelevant_transitions - checked_transitions]]
        print(tabulate(table, headers='firstrow', tablefmt='fancy_grid'))
        file_name = "/mnt/c/repos/sawtooth-giskard/tests/sawtooth_poet_tests/info_table.txt"
        f = open(file_name, 'a')
        f.write("\n" + tabulate(table, headers='firstrow', tablefmt='fancy_grid'))
        f.close()

    @staticmethod
    def get_nr_of_states_per_node(gtrace: GTrace, nodes) -> dict:
        final_gstate = gtrace.gtrace[-1]
        node_states = {}
        for node in nodes:
            node_states[node] = len(final_gstate.gstate[node])
        return node_states

    @staticmethod
    def get_nodes_next_state(gtrace: GTrace, i: int, node: str):
        """ the gtrace is updated after every local state transition,
        so only one node will have a new transition within the next gstate
        :returns the next gstate where the node has its next transition """
        tmp_gstate = gtrace.gtrace[i]
        for j in range(i, len(gtrace.gtrace)):
            try:
                if gtrace.gtrace[j].gstate[node][-1] != tmp_gstate.gstate[node][-1]:
                    return gtrace.gtrace[j]
            except KeyError as e:
                print(e)
        return None

    @staticmethod
    def get_nr_of_transitions(file_name):
        f = open(file_name)
        content = f.read()
        nr = jsonpickle.decode(content)
        f.close()
        return int(nr)

    @staticmethod
    def inc_nr_of_transitions(file_name):
        nr = Giskard.get_nr_of_transitions(file_name)
        nr += 1
        f = open(file_name, "w")
        f.write(str(nr))
        f.close()

    @staticmethod
    def reset_nr_of_transitions(file_name):
        f = open(file_name, "w")
        f.write(str(0))
        f.close()

    @staticmethod
    def PrepareVote_about_block_in_view_global(gtrace: GTrace, i: int,
                                               block: GiskardBlock, view: int) -> List[GiskardMessage]:
        """ Returns all broadcasted PrepareVote msgs for a block in the given view. """
        return list(filter(lambda msg: msg.message_type == GiskardMessage.CONSENSUS_GISKARD_PREPARE_VOTE
                                       and msg.block == block
                                       and msg.view == view, gtrace[i].broadcast_msgs))

    @staticmethod
    def vote_quorum_in_view_global(gtrace: GTrace, i: int, block: GiskardBlock, view: int, peers) -> bool:
        """ Returns True if there is a quorum for the global PrepareVote messages. """
        return Giskard.quorum(Giskard.PrepareVote_about_block_in_view_global(gtrace, i, block, view), peers)

    # endregion

    # region safety property one: prepare stage height injectivity
    @staticmethod
    def prepare_stage_same_view_height_injective_statement(gtraces: List[GTrace], peers) -> bool:
        """ Prepare stage height injectivity definition """

        """ Recall that a block is in prepare stage in some local state if it has
        received quorum PrepareVote messages or a PrepareQC message in the current
        view or some previous view.

        The first safety property states that no two same height blocks can be
        at prepare stage in the same view, i.e., prepare stage block height is injective
        in the same view. The first safety property differs from the following two in that
        it contains an additional premise restricting the view during which the two blocks
        reached prepare stage. This is important because it is possible for multiple blocks
        at the same height to reach prepare stage across different views in the case of
        abnormal view changes.

        In any global state i in a valid protocol trace tr that begins with the initial
        state and respects the protocol transition rules, if there are two participating
        nodes n and m, and two blocks b1 b2, such that b1 and b2 have the same height,
        and both reach prepare stage for n and m's local state respectively in some view p,
        but are not equal, then we can prove a contradiction. """
        for gtrace in gtraces:
            for i in range(0, len(gtrace.gtrace)):  # iterating through the global states in the trace
                gstate = gtrace.gtrace[i].gstate
                nodes = set(gstate.keys())
                for node1, node2 in itertools.product(nodes, nodes):
                    if node1 == node2:
                        continue
                    blocks1 = Giskard.create_mock_block_cache_from_counting_msgs(gstate[node1][-1],
                                                                                 None,
                                                                                 peers).block_store.uncommitted_blocks
                    blocks2 = Giskard.create_mock_block_cache_from_counting_msgs(gstate[node2][-1],
                                                                                 None,
                                                                                 peers).block_store.uncommitted_blocks
                    view = min(gstate[node1][-1].node_view, gstate[node2][-1].node_view)
                    for v, block1, block2 in itertools.product(range(0, view + 1), blocks1,
                                                               blocks2):  # Todo check if views start with 0 or 1
                        # if block1.block_num == 3 and block2.block_num == 3:
                        #    import pdb;
                        #    pdb.set_trace()
                        if node1 in peers \
                                and node2 in peers \
                                and block1 != block2 \
                                and Giskard.prepare_stage_in_view(gstate[node1][-1], v, block1, peers) \
                                and Giskard.prepare_stage_in_view(gstate[node2][-1], v, block2, peers) \
                                and block1.block_num == block2.block_num:  # blocks are different but same block height
                            return False  # TODO more printout info would be nice for testing
        return True

    """ TODO check if the three @staticmethod
        def prepare_stage_same_view_height_injective_statement(gtraces: List[GTrace], peers):
            for gtrace in gtraces:
                for i in range(0, len(gtrace) - 1):
                    gstate = gtrace[i].gstate
                    nodes = list(gstate.keys())
                    view = gstate[nodes[0]].node_view
                    for v in range(1, view):  # TODO is there a view 0 except for the genesis block?
                        for n in range(0, len(nodes) - 1):
                            if n < len(nodes) - 1:  # we are comparing two different nodes, are there not more are we done
                                node1 = nodes[n]
                                node2 = nodes[n + 1]
                                blocks1 = Giskard.create_mock_block_cache_from_counting_msgs(gstate[node1], peers)
                                blocks2 = Giskard.create_mock_block_cache_from_counting_msgs(gstate[node1], peers)
                                for block1 in blocks1:
                                    for block2 in blocks2:
                                        if node1 in peers \
                                                and node2 in peers \
                                                and block1 != block2 \
                                                and Giskard.prepare_stage_in_view(gstate[node1][0], v, block1) \
                                                and Giskard.prepare_stage_in_view(gstate[node2][0], v, block2) \
                                                and block1.block_num == block2.block_num:  # same block height
                                            return False
            return True """

    """ TODO check if the three return the same thing @staticmethod
    def prepare_stage_same_view_height_injective_statement(gtraces: List[GTrace], peers):
        for gtrace in gtraces:
            for i in range(0, len(gtrace) - 1):
                gstate = gtrace[i].gstate
                nodes = set(gstate.keys())
                view = gstate[nodes.pop()].node_view
                for v in range(1, view):  # TODO is there a view 0 except for the genesis block?
                    while nodes:
                        node1 = nodes.pop()
                        blocks1 = Giskard.create_mock_block_cache_from_counting_msgs(gstate[node1], peers)
                        for node2 in nodes:
                            blocks2 = Giskard.create_mock_block_cache_from_counting_msgs(gstate[node2], peers)
                            for block1 in blocks1:
                                for block2 in blocks2:
                                    if node1 in peers \
                                            and node2 in peers \
                                            and block1 != block2 \
                                            and Giskard.prepare_stage_in_view(gstate[node1][0], v, block1) \
                                            and Giskard.prepare_stage_in_view(gstate[node2][0], v, block2) \
                                            and block1.block_num == block2.block_num:  # same block height
                                        return False
        return True """

    """ Intuitively, the proof of this property follows directly from the definition of
    non-Byzantine/honest voting behavior: honest nodes cannot vote for two conflicting
    blocks during the same view. If two conflicting blocks reach prepare stage in the
    same view, then two quorums of at least 2/3 validators voted for each block.
    By the pigeonhole principle, there must exist a set of at least 1/3 validators who
    voted for both conflicting blocks and are therefore Byzantine/dishonest. By assumption,
    there are no more than 1/3 Byzantine/dishonest blocks. Therefore, we reach a contradiction. """

    """ TODO Reducing the proof? """

    # endregion

    # region safety property two: precommit stage height injectivity
    @staticmethod
    def precommit_stage(gtrace: GTrace, i: int, node: GiskardNode, block: GiskardBlock, peers) -> bool:
        """ Precommit stage definition
        We say that a block is in precommit stage in some local state iff it is in
        prepare stage, and its child block is in prepare stage, i.e., it has received
        quorum PrepareVote messages or a PrepareQC message in the current view or
        some previous view. """
        child_block = node.block_cache.block_store.get_child_block(block)
        parent_block = None
        if child_block is not None:
            parent_block = node.block_cache.block_store.get_parent_block(child_block)
        return child_block is not None and parent_block is not None and block is not None \
            and parent_block == block \
            and Giskard.prepare_stage(gtrace.gtrace[i].gstate[node.node_id][-1], block, peers) \
            and Giskard.prepare_stage(gtrace.gtrace[i].gstate[node.node_id][-1], child_block, peers)

    @staticmethod
    def precommit_stage_height_injective_statement(gtraces: List[GTrace], peers) -> bool:
        """ Precommit stage height injectivity statement
        The second safety property states that no two same height blocks can be at
        precommit stage, i.e., precommit stage block height is injective. The second safety
        property does not require that the two blocks reach precommit stage in the same view. *)

        (** More formally, in any global state i in a valid protocol trace tr that begins
        with the initial state and respects the protocol transition rules, if there are
        two participating nodes n and m, and two blocks b1 b2, such that b1 and b2 have
        the same height, and are both in precommit stage in n and m's local state respectively,
        but are not equal, then we can prove a contradiction. """
        for gtrace in gtraces:
            for i in range(0, len(gtrace.gtrace)):  # iterating through the global states in the trace
                gstate = gtrace.gtrace[i].gstate
                nodes = set(gstate.keys())
                for node1, node2 in itertools.product(nodes, nodes):
                    if node1 == node2:
                        continue
                    block_cache1 = Giskard.create_mock_block_cache_from_counting_msgs(gstate[node1][-1], None, peers)
                    blocks1 = block_cache1.block_store.uncommitted_blocks
                    block_cache2 = Giskard.create_mock_block_cache_from_counting_msgs(gstate[node2][-1], None, peers)
                    blocks2 = block_cache2.block_store.uncommitted_blocks
                    full_node1 = GiskardNode(node1, False, block_cache1)
                    full_node2 = GiskardNode(node2, False, block_cache2)
                    for block1, block2 in itertools.product(blocks1, blocks2):  # Todo check if views start with 0
                        if node1 in peers \
                                and node2 in peers \
                                and block1 != block2 \
                                and Giskard.precommit_stage(gtrace, i, full_node1, block1, peers) \
                                and Giskard.precommit_stage(gtrace, i, full_node2, block2, peers) \
                                and block1.block_num == block2.block_num:  # blocks are different but same block height
                            return False  # TODO more printout info would be nice for testing
        return True

    # endregion

    # region safety property three: commit stage height injectivity
    @staticmethod
    def commit_stage(gtrace: GTrace, i: int, node: GiskardNode, block: GiskardBlock, peers) -> bool:
        """ Commit stage definition

        We say that a block is in commit stage in some local state iff it is in precommit stage
        and its child block is in precommit stage."""
        child_block = node.block_cache.block_store.get_child_block(block)
        return child_block is not None \
            and node.block_cache.block_store.get_parent_block(child_block) == block \
            and Giskard.precommit_stage(gtrace, i, node, block, peers) \
            and Giskard.precommit_stage(gtrace, i, node, child_block, peers)

    @staticmethod
    def commit_height_injective_statement(gtraces: List[GTrace], peers) -> bool:
        """ Commit stage height injectivity statement

        The third and final safety property states that no two same height blocks can be at
        commit stage, i.e., commit stage block height is injective.

        In any global state <<i>> in a valid protocol trace <<tr>> that begins with the
        initial state and respects the protocol transition rules, if there are two participating
        nodes <<n>> and <<m>>, and two distinct blocks <<b1>> and <<b2>> of the same height
        that are both at precommit stage for <<n>> and <<m>>'s local state, respectively,
        then we can prove a contradiction. """
        for gtrace in gtraces:
            for i in range(0, len(gtrace.gtrace)):  # iterating through the global states in the trace
                gstate = gtrace.gtrace[i].gstate
                nodes = set(gstate.keys())
                for node1, node2 in itertools.product(nodes, nodes):
                    if node1 == node2:
                        continue
                    block_cache1 = Giskard.create_mock_block_cache_from_counting_msgs(gstate[node1][-1], None, peers)
                    blocks1 = block_cache1.block_store.uncommitted_blocks
                    block_cache2 = Giskard.create_mock_block_cache_from_counting_msgs(gstate[node2][-1], None, peers)
                    blocks2 = block_cache2.block_store.uncommitted_blocks
                    full_node1 = GiskardNode(node1, False, block_cache1)
                    full_node2 = GiskardNode(node2, False, block_cache2)
                    for block1, block2 in itertools.product(blocks1, blocks2):  # Todo check if views start with 0
                        if node1 in peers \
                                and node2 in peers \
                                and block1 != block2 \
                                and Giskard.commit_stage(gtrace, i, full_node1, block1, peers) \
                                and Giskard.commit_stage(gtrace, i, full_node2, block2, peers) \
                                and block1.block_num == block2.block_num:  # blocks are different but same block height
                            return False  # TODO more printout info would be nice for testing
        return True

    # endregion
