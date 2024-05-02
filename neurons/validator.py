# The MIT License (MIT)
# Copyright © 2024 Afterparty, Inc.

# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the “Software”), to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all copies or substantial portions of
# the Software.

# THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
# THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.


import time
import os
import hashlib
import random

# Bittensor
import bittensor as bt
#bt.logging.enable_debug(True)

# Bittensor Validator Template:
#import template
#from template.validator import forward

# import base validator class which takes care of most of the boilerplate
from conversationgenome.base.validator import BaseValidatorNeuron

import conversationgenome.utils
import conversationgenome.validator

from conversationgenome.ConfigLib import c
from conversationgenome.Utils import Utils

from conversationgenome.WandbLib import WandbLib

from conversationgenome.ValidatorLib import ValidatorLib
from conversationgenome.validator.evaluator import Evaluator

from conversationgenome.protocol import CgSynapse

class Validator(BaseValidatorNeuron):
    verbose = True
    """
    Keeping a moving average of the scores of the miners and using them to set weights at the end of each epoch. Additionally, the scores are reset for new hotkeys at the end of each epoch.
    """

    def __init__(self, config=None):
        super(Validator, self).__init__(config=config)

        bt.logging.info("load_state()")
        self.load_state()



    async def forward(self):
        wl = WandbLib()

        # Get random miner sample size
        miners_per_window = c.get("validator", "miners_per_window", 3)
        miner_sample_size = min(self.config.neuron.sample_size, self.metagraph.n.item())
        print("miner_sample_size", miner_sample_size, self.config.neuron.sample_size, self.metagraph.n.item())

        # Get hotkeys to watch for debugging
        hot_keys = c.get("env", "HIGHLIGHT_HOTKEYS", "")
        hot_key_watchlist = hot_keys.split(",")

        # Instance of validator and eval library
        vl = ValidatorLib()
        el = Evaluator()
        # xxx -- what does this do?
        vl.validateMinimumTags([])
        test_mode = True

        # Reserve a conversation (so others won't take it) from the conversation API
        result = await vl.reserve_conversation()

        if result:
            (full_conversation, full_conversation_metadata, conversation_windows) = result
            if test_mode:
                # In test_mode, to expand the miner scores, remove half of the full convo tags.
                # This "generates" more unique tags found for the miners
                half = int(len(full_conversation_metadata['tags'])/2)
                #full_conversation_metadata['tags'] = full_conversation_metadata['tags'][0:half]
            conversation_guid = Utils.get(full_conversation, "guid")
            #print("full_conversation", full_conversation)
            bt.logging.info(f"Received {len(conversation_windows)} conversation_windows from API")


            llm_type = c.get("env", "LLM_TYPE")
            model = c.get("env", "OPENAI_MODEL")
            full_conversation_tag_count = len(Utils.get(full_conversation_metadata, "tags", []))
            lines = Utils.get(full_conversation, "lines", [])
            participants = Utils.get(full_conversation, "participants", [])
            miners_per_window = c.get("validator", "miners_per_window", 3)
            min_lines = c.get("convo_window", "min_lines", 5)
            max_lines = c.get("convo_window", "max_lines", 10)
            overlap_lines = c.get("convo_window", "overlap_lines", 2)
            batch_num = random.randint(100000, 9999999)
            await vl.put_convo("FINDHOTKEY", conversation_guid, full_conversation_metadata, type="validator",  batch_num=batch_num, window=999)

            wl.log({
               "llm_type": llm_type,
               "model": model,
               "conversation_guid": conversation_guid,
               "full_convo_tag_count": full_conversation_tag_count,
               "num_lines": len(lines),
               "num_participants": len(participants),
               "num_convo_windows": len(conversation_windows),
               "convo_windows_min_lines": min_lines,
               "convo_windows_max_lines": max_lines,
               "convo_windows_overlap_lines": overlap_lines,
            })


            # Loop through conversation windows. Send each window to multiple miners
            print(f"Found {len(conversation_windows)} conversation windows. Sequentially sending to batches of miners")
            for window_idx, conversation_window in enumerate(conversation_windows):
                miner_uids = conversationgenome.utils.uids.get_random_uids(
                    self,
                    k= miner_sample_size
                )
                if self.verbose:
                    print("miner_uid pool", miner_uids)
                if len(miner_uids) == 0:
                    print("No miners")
                    time.sleep(30)
                    return

                # Create a synapse to distribute to miners
                print(f"Sending convo {conversation_guid} window {window_idx} of {len(conversation_window)} lines to miner.")
                window_packet = {"guid":conversation_guid, "window_idx":window_idx, "lines":conversation_window}
                #print(window_packet)

                synapse = conversationgenome.protocol.CgSynapse(cgp_input = [window_packet])

                rewards = None

                responses = self.dendrite.query(
                    axons=[self.metagraph.axons[uid] for uid in miner_uids],
                    synapse=synapse,
                    deserialize=False,
                )
                #print("RAW RESPONSES", len(responses))
                #valid_responses = []
                for window_idx, response in enumerate(responses):
                    if not response.cgp_output:
                        print("BAD RESPONSE", response.axon.uuid, response.axon.hotkey, response.cgp_output)
                        if response.axon.hotkey in hot_key_watchlist:
                            print(f"!!!!!!!!!!! BAD WATCH: {response.axon.hotkey} !!!!!!!!!!!!!")
                        continue
                    print("GOOD RESPONSE", response.axon.uuid, response.axon.hotkey, response.axon,  )
                    if response.axon.hotkey in hot_key_watchlist:
                        print(f"!!!!!!!!!!! GOOD WATCH: {response.axon.hotkey} !!!!!!!!!!!!!")
                    print(f"CGP Received tags: {response.cgp_output[0]['tags']} -- PUTTING OUTPUT")
                    await vl.put_convo(response.axon.hotkey, conversation_guid, response.cgp_output[0], type="miner",  batch_num=batch_num, window=window_idx)
                #    valid_responses.append(response.cgp_output[0])

                #for miner_result in valid_responses:
                #    #print("miner_result", miner_result)
                #    bt.logging.info(f"MINER RESULT uid: {miner_result['uid']}, tags: {miner_result['tags']} vector count: {len(miner_result['vectors'])}")
                (final_scores, rank_scores) = await el.evaluate(full_convo_metadata=full_conversation_metadata, miner_responses=responses)
                for idx, score in enumerate(final_scores):
                    print("score", score)
                    uid = str(Utils.get(score, "uuid"))
                    wl.log({
                        "conversation_guid."+uid: conversation_guid,
                        "window_id."+uid: window_idx,
                        "uuid."+uid: Utils.get(score, "uuid"),
                        "hotkey."+uid: Utils.get(score, "hotkey"),
                        "adjusted_score."+uid: Utils.get(score, "adjustedScore"),
                        "final_miner_score."+uid: Utils.get(score, "final_miner_score"),
                    })
                    #print("^^^^^^RANK", final_scores, rank_scores, len(final_scores), miner_uids)

                # Update the scores based on the rewards.
                self.update_scores(rank_scores, miner_uids)

# The main function parses the configuration and runs the validator.
if __name__ == "__main__":
    wl = WandbLib()
    wl.init_wandb()

    try:
        with Validator() as validator:
            while True:
                bt.logging.info("CGP Validator running...", time.time())
                # xxx Remove for Prod? Add for mode test?
                time.sleep(5)
    except KeyboardInterrupt:
        bt.logging.info("Keyboard interrupt detected. Exiting validator.")
    finally:
        print("Done. Writing final to wandb.")
        wl.end_log_wandb()
