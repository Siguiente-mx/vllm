import asyncio
import codecs
from http import HTTPStatus
from typing import Dict, List, Optional, Tuple, Union
from vllm.logger import init_logger
from vllm.transformers_utils.tokenizer import get_tokenizer
from vllm.engine.async_llm_engine import AsyncLLMEngine
from vllm.entrypoints.openai.protocol import (CompletionRequest,
                                              ChatCompletionRequest,
                                              ErrorResponse, LogProbs,
                                              ModelCard, ModelList,
                                              ModelPermission)

logger = init_logger(__name__)


class OpenAIServing:

    def __init__(self,
                 engine: AsyncLLMEngine,
                 served_model: str,
                 response_role: str,
                 chat_template=None):
        self.engine = engine
        self.served_model = served_model
        self.chat_template = chat_template
        self.response_role = response_role

        self.max_model_len = 0
        self.tokenizer = None

        if engine.engine_use_ray:
            post_init_task = asyncio.ensure_future(self._post_init())
            asyncio.get_event_loop().run_until_complete(
                asyncio.gather(post_init_task))
        else:
            asyncio.run(self._post_init())

    async def _post_init(self):
        engine_model_config = await self.engine.get_model_config()
        self.max_model_len = engine_model_config.max_model_len

        # A separate tokenizer to map token IDs to strings.
        self.tokenizer = get_tokenizer(
            engine_model_config.tokenizer,
            tokenizer_mode=engine_model_config.tokenizer_mode,
            trust_remote_code=engine_model_config.trust_remote_code)
        self._load_chat_template(self.chat_template)

    async def show_available_models(self) -> ModelList:
        """Show available models. Right now we only have one model."""
        model_cards = [
            ModelCard(id=self.served_model,
                      root=self.served_model,
                      permission=[ModelPermission()])
        ]
        return ModelList(data=model_cards)

    def _create_logprobs(
        self,
        token_ids: List[int],
        top_logprobs: Optional[List[Optional[Dict[int, float]]]] = None,
        num_output_top_logprobs: Optional[int] = None,
        initial_text_offset: int = 0,
    ) -> LogProbs:
        """Create OpenAI-style logprobs."""
        logprobs = LogProbs()
        last_token_len = 0
        if num_output_top_logprobs:
            logprobs.top_logprobs = []
        for i, token_id in enumerate(token_ids):
            step_top_logprobs = top_logprobs[i]
            if step_top_logprobs is not None:
                token_logprob = step_top_logprobs[token_id]
            else:
                token_logprob = None
            token = self.tokenizer.convert_ids_to_tokens(token_id)
            logprobs.tokens.append(token)
            logprobs.token_logprobs.append(token_logprob)
            if len(logprobs.text_offset) == 0:
                logprobs.text_offset.append(initial_text_offset)
            else:
                logprobs.text_offset.append(logprobs.text_offset[-1] +
                                            last_token_len)
            last_token_len = len(token)

            if num_output_top_logprobs:
                logprobs.top_logprobs.append({
                    self.tokenizer.convert_ids_to_tokens(i): p
                    for i, p in step_top_logprobs.items()
                } if step_top_logprobs else None)
        return logprobs

    def create_error_response(
            self,
            message: str,
            err_type: str = "BadRequestError",
            status_code: HTTPStatus = HTTPStatus.BAD_REQUEST) -> ErrorResponse:
        return ErrorResponse(message=message,
                             type=err_type,
                             code=status_code.value)

    async def _check_model(self, request) -> Optional[ErrorResponse]:
        if request.model == self.served_model:
            return
        return self.create_error_response(
            message=f"The model `{request.model}` does not exist.",
            err_type="NotFoundError",
            status_code=HTTPStatus.NOT_FOUND)

    async def _check_length(
        self,
        request: Union[ChatCompletionRequest, CompletionRequest],
        prompt: Optional[str] = None,
        prompt_ids: Optional[List[int]] = None
    ) -> Tuple[List[int], Optional[ErrorResponse]]:
        assert (not (prompt is None and prompt_ids is None)
                and not (prompt is not None and prompt_ids is not None)
                ), "Either prompt or prompt_ids should be provided."
        input_ids = prompt_ids if prompt_ids is not None else self.tokenizer(
            prompt).input_ids
        token_num = len(input_ids)

        if request.max_tokens is None:
            request.max_tokens = self.max_model_len - token_num
        if token_num + request.max_tokens > self.max_model_len:
            return input_ids, self.create_error_response(
                f"This model's maximum context length is {self.max_model_len} tokens. "
                f"However, you requested {request.max_tokens + token_num} tokens "
                f"({token_num} in the messages, "
                f"{request.max_tokens} in the completion). "
                f"Please reduce the length of the messages or completion.", )
        else:
            return input_ids, None

    def _load_chat_template(self, chat_template):
        if chat_template is not None:
            try:
                with open(chat_template, "r") as f:
                    self.tokenizer.chat_template = f.read()
            except OSError:
                # If opening a file fails, set chat template to be args to
                # ensure we decode so our escape are interpreted correctly
                self.tokenizer.chat_template = codecs.decode(
                    chat_template, "unicode_escape")

            logger.info(
                f"Using supplied chat template:\n{self.tokenizer.chat_template}"
            )
        elif self.tokenizer.chat_template is not None:
            logger.info(
                f"Using default chat template:\n{self.tokenizer.chat_template}"
            )
        else:
            logger.warning(
                "No chat template provided. Chat API will not work.")
