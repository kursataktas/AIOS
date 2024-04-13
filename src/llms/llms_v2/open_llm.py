import torch
from .constant import MODEL_CLASS
from .base_llm import BaseLLMKernel
import time
from transformers import AutoTokenizer

class OpenLLM(BaseLLMKernel):

    def load_llm_and_tokenizer(self) -> None:
        self.model_type = self.config["model_type"]
        self.model_name = self.config["model_name"]
        self.max_gpu_memory = self.convert_map(self.max_gpu_memory)
        hf_token = self.config["hf_token"] if "hf_token" in self.config.keys() else None
        cache_dir = self.config["cache_dir"] if "cache_dir" in self.config.keys() else None
        self.model = MODEL_CLASS[self.model_type].from_pretrained(
            self.model_name,
            use_auth_token=hf_token,
            cache_dir=cache_dir,
            # torch_dtype=torch.float16,
            # load_in_8bit = True,
            device_map="auto",
            max_memory=self.max_gpu_memory
        )
        # self.model = self.model.to(self.eval_device)
        self.tokenizer = AutoTokenizer.from_pretrained(
            self.model_name,
            use_auth_token=hf_token,
            cache_dir=cache_dir
        )
        # print(f"EOS token id: {self.model.config.eos_token_id}")
        self.tokenizer.pad_token_id = self.tokenizer.eos_token_id

    def process(self,
                agent_process,
                temperature=0.0) -> None:
        if self.context_manager.check_restoration(agent_process.get_pid()):
            restored_context = self.context_manager.gen_recover(
                agent_process.get_pid()
            )
            start_idx = restored_context["start_idx"]
            beams = restored_context["beams"]
            beam_scores = restored_context["beam_scores"]
            beam_attention_masks = restored_context["beam_attention_masks"]

            outputs = self.generate(
                search_mode = "beam_search",
                beam_size = 1,
                beams = beams,
                beam_scores = beam_scores,
                beam_attention_masks = beam_attention_masks,
                max_new_tokens = self.MAX_NEW_TOKENS,
                start_idx = start_idx,
                timestamp = agent_process.get_time_limit()
            )
        else:
            prompt = agent_process.prompt
            input_ids = self.tokenizer.encode(prompt, return_tensors="pt")
            attention_masks = input_ids != self.tokenizer.pad_token_id
            input_ids = input_ids.to(self.eval_device)
            attention_masks = attention_masks.to(self.eval_device)

            outputs = self.generate(
                input_ids = input_ids,
                attention_masks = attention_masks,
                search_mode = "beam_search",
                beam_size = 1,
                max_new_tokens=self.MAX_NEW_TOKENS,
                start_idx = 0,
                timestamp = agent_process.get_time_limit()
            )

        output_ids = outputs["result"]

        # print(f"Output ID: {output_ids}")
        prompt = agent_process.prompt
        result = self.tokenizer.decode(output_ids, skip_special_tokens=True)
        # print(result)
        result = result[len(prompt)+1: ]

        if outputs["finished_flag"]: # finished flag is set as True

            # print("Finished")
            if self.context_manager.check_restoration(
                agent_process.get_pid()):
                self.context_manager.clear_restoration(
                    agent_process.get_pid()
                )
            # print(f"{agent_process.agent_name} done: {result}")
            agent_process.set_response(result)
            agent_process.set_status("done")

        else:
            # print(f"{agent_process.agent_name} suspended: {result}")
            # self.logger.info(f"[{agent_process.agent_name}] is suspended due to the time limit.")
            # print(f'{agent_process.get_pid()}: {outputs["start_idx"]}')
            self.context_manager.gen_snapshot(
                agent_process.get_pid(),
                context = {
                    "start_idx": outputs["start_idx"],
                    "beams": outputs["beams"],
                    "beam_scores": outputs["beam_scores"],
                    "beam_attention_masks": outputs["beam_attention_masks"]
                }
            )
            agent_process.set_response(result)
            agent_process.set_status("suspended")

        agent_process.set_end_time(time.time())

    def generate(self,
                 input_ids: torch.Tensor = None,
                 attention_masks: torch.Tensor = None,
                 beams: torch.Tensor = None,
                 beam_scores: torch.Tensor = None,
                 beam_attention_masks: torch.Tensor = None,
                 beam_size: int = None,
                 max_new_tokens: int = None,
                 search_mode: str = None,
                 start_idx: int = 0,
                 timestamp: int = None
                 ):
        if search_mode == "beam_search":
            output_ids = self.beam_search(
                input_ids = input_ids,
                attention_masks = attention_masks,
                beam_size = beam_size,
                beams = beams,
                beam_scores = beam_scores,
                beam_attention_masks = beam_attention_masks,
                max_new_tokens = max_new_tokens,
                start_idx = start_idx,
                timestamp = timestamp
            )
            return output_ids
        else:
            return NotImplementedError

    def beam_search(self,
                    input_ids: torch.Tensor = None,
                    attention_masks: torch.Tensor = None,
                    beams=None,
                    beam_scores=None,
                    beam_attention_masks=None,
                    beam_size: int = None,
                    max_new_tokens: int = None,
                    start_idx: int = 0,
                    timestamp: int = None
                    ):
        # num_sequences = input_ids.shape[0]
        # num_sequences = input_ids.shape[0]
        if beams is None or beam_scores is None and beam_attention_masks is None:
            # beams = [input_ids.clone() for _ in range(beam_size)]
            # beam_scores = torch.zeros(beam_size, device=self.eval_device)
            # beam_attention_masks = [attention_masks.clone() for _ in range(beam_size)]
            beams = input_ids.repeat(beam_size, 1)
            beam_attention_masks = attention_masks.repeat(beam_size, 1)
            beam_scores = torch.zeros(beam_size, device=self.eval_device)

        start_time = time.time()

        finished_flag = False

        idx = start_idx

        # print(start_idx)

        for step in range(start_idx, max_new_tokens):
            with torch.no_grad():
                # Obtain logits for the last tokens across all beams
                outputs = self.model(beams, attention_mask=beam_attention_masks)
                next_token_logits = outputs.logits[:, -1, :]

                # Apply softmax to convert logits to probabilities
                next_token_probs = torch.softmax(next_token_logits, dim=-1)

            # Calculate scores for all possible next tokens for each beam
            next_token_scores = beam_scores.unsqueeze(-1) + torch.log(next_token_probs)

            # Flatten to treat the beam and token dimensions as one
            next_token_scores_flat = next_token_scores.view(-1)

            # Select top overall scores to find the next beams
            top_scores, top_indices = torch.topk(next_token_scores_flat, beam_size, sorted=True)

            # Determine the next beams and their corresponding tokens
            beam_indices = top_indices // next_token_probs.size(1)  # Find which beam the top tokens came from
            token_indices = top_indices % next_token_probs.size(1)  # Find which token within the beam was selected

            # Update beams, scores, and attention masks
            beams = torch.cat([beams[beam_indices], token_indices.unsqueeze(-1)], dim=-1)
            beam_attention_masks = torch.cat([beam_attention_masks[beam_indices], torch.ones_like(token_indices).unsqueeze(-1)], dim=-1)
            beam_scores = top_scores

            # Check for stopping criteria
            if timestamp is not None and time.time() - start_time >= timestamp:
                idx = step
                break

            # Check for completion
            if torch.all(beams[:, -1] == self.tokenizer.eos_token_id):
                idx = step
                finished_flag = True
                break

            if step + 1 == max_new_tokens:
                idx = step
                finished_flag = True
                break

        # print(idx)
        # if finished_flag:
        best_beam_idx = beam_scores.argmax()

        best_beam = beams[best_beam_idx]

        outputs = {
            "finished_flag": finished_flag,
            "start_idx": idx,
            "beams": beams,
            "beam_scores": beam_scores,
            "beam_attention_masks": beam_attention_masks,
            "result": best_beam
        }

        return outputs
