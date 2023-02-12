import re
import html
from collections import namedtuple
from itertools import zip_longest
from typing import Generator
import gradio as gr
import open_clip.tokenizer
from ldm.modules.encoders.modules import FrozenCLIPEmbedder, FrozenOpenCLIPEmbedder

from modules import scripts, shared, sd_hijack, prompt_parser, extra_networks
from modules.sd_hijack_clip import PromptChunk


class VanillaClip:
    def __init__(self, clip):
        self.clip = clip

    def vocab(self):
        return self.clip.tokenizer.get_vocab()

    def byte_decoder(self):
        return self.clip.tokenizer.byte_decoder


class OpenClip:
    def __init__(self, clip):
        self.clip = clip
        self.tokenizer = open_clip.tokenizer._tokenizer

    def vocab(self):
        return self.tokenizer.encoder

    def byte_decoder(self):
        return self.tokenizer.byte_decoder


# あるStepのプロンプト
ScheduledPrompt = namedtuple("ScheduledPrompt", ["end_at_step", "prompt"])


# ANDで分割した1つのプロンプトのStep毎の変化を時系列順に並べたもの
class ComposableScheduledPrompt:
    def __init__(self, schedules: list[list[int | str]], weight=1.0):
        self.schedules: list[ScheduledPrompt] = [ScheduledPrompt(step, prompt) for step, prompt in schedules]
        self.weight: float = weight


# AND(Composable Diffusion)で分割した1つのプロンプトのStep毎の変化(Prompt editing, Alternating Words)を時系列順に並べる
def get_multi_prompt_schedules(text, steps) -> list[ComposableScheduledPrompt]:
    try:
        text, _ = extra_networks.parse_prompt(text)
        [indexes], prompt_flat_list, prompt_indexes = prompt_parser.get_multicond_prompt_list([text])
        prompt_schedules = prompt_parser.get_learned_conditioning_prompt_schedules(prompt_flat_list, steps)
        return [ComposableScheduledPrompt(prompt_schedules[i], weight) for i, weight in indexes]

    except Exception:
        return [ComposableScheduledPrompt([[steps, text]])]


class CustomPromptChunk:
    def __init__(self, original_chunk: PromptChunk):
        # startとendのトークンを除く
        def tokens_subset(tokens: list[int]):
            return tokens[1:tokens.index(sd_hijack.model_hijack.clip.id_end)] if len(tokens) else []

        self.tokens: list[int] = tokens_subset(original_chunk.tokens)
        self.token_count: int = len(self.tokens)
        self.multipliers: list[float] = original_chunk.multipliers[1:self.token_count+1]


class ScheduledChunk:
    def __init__(self, schedules: list[CustomPromptChunk] | tuple[CustomPromptChunk, ...]):
        self.schedules = schedules
        self.chunk = schedules[0]
        self.token_counts = [(schedule.token_count if schedule else 0) for schedule in schedules]


def get_scheduled_tokenized_chunks(schedules: list[ScheduledPrompt]) -> Generator[ScheduledChunk, None, None]:
    def inner_tokenize(prompt: str) -> tuple[CustomPromptChunk, ...]:
        nonlocal hijacked_clip
        return tuple(CustomPromptChunk(chunk) for chunk in hijacked_clip.tokenize_line(prompt)[0])

    hijacked_clip = sd_hijack.model_hijack.clip
    zipped = (zip_longest(*(inner_tokenize(schedule.prompt) for schedule in schedules), fillvalue=CustomPromptChunk(PromptChunk())))
    return (ScheduledChunk(schedules) for schedules in zipped)


def convert_chunks_to_html(clip, scheduled_chunks: Generator[ScheduledChunk, None, None]) -> Generator[str, None, None]:
    vocab = {v: k for k, v in clip.vocab().items()}
    byte_decoder = clip.byte_decoder()
    hijacked_clip = sd_hijack.model_hijack.clip

    for scheduled_chunk in scheduled_chunks:
        code, ids, current_ids, class_index = '', [], [], 0

        # TODO: 中身をあんま理解できてないのでどうにかしたい
        def dump(last=False, _multiplier=1.0):
            nonlocal code, ids, current_ids

            words = [vocab.get(x, "") for x in current_ids]

            def wordscode(_ids, _word):
                nonlocal class_index

                token_ids_text = html.escape(", ".join([str(x) for x in _ids]))
                space = " " if "</w>" in _word else ""
                _word = _word.replace("</w>", "")
                html_class = f"i-tokenizer-token i-tokenizer-token-{class_index % 4}"
                if _multiplier <= 1.0:
                    css_style = f"background: rgba(0, 0, 255, {1.0 - _multiplier});"
                else:
                    css_style = f"background: rgba(255, 0, 0, {(_multiplier - 1.0) / 3.0});"
                res = f"""<span class='{html_class}' title='{token_ids_text}' style='{css_style}'>{html.escape(_word)}</span>{space}"""
                class_index += 1
                return res

            try:
                word = bytearray([byte_decoder[x] for x in ''.join(words)]).decode("utf-8")
            except UnicodeDecodeError:
                if last:
                    word = "❌" * len(current_ids)
                elif len(current_ids) > 4:
                    id = current_ids[0]
                    ids += [id]
                    local_ids = current_ids[1:]
                    code += wordscode([id], "❌")

                    current_ids = []
                    for id in local_ids:
                        current_ids.append(id)
                        dump()

                    return
                else:
                    return

            code += wordscode(current_ids, word)
            ids += current_ids

            current_ids = []

        for token, multiplier in zip(scheduled_chunk.chunk.tokens, scheduled_chunk.chunk.multipliers):
            # TODO: tokenは元々intなのか、そうじゃないからキャストしているのかよくわからない
            token = int(token)
            current_ids.append(token)

            dump(_multiplier=multiplier)

        dump(last=True)

        schedules_count = len(scheduled_chunk.token_counts)
        joined_token_counts = "->".join(str(count) for count in scheduled_chunk.token_counts[:5])
        token_counts_tail = "->..." if 5 < schedules_count else ""
        token_counts_html = f"{joined_token_counts}{token_counts_tail} / {hijacked_clip.chunk_length}"
        yield f"""<p>{code}<span class='i-tokenizer-count'>{token_counts_html}</span></p>"""


def tokenize(text, negative: bool = False):
    steps = 30
    clip = shared.sd_model.cond_stage_model.wrapped
    if isinstance(clip, FrozenCLIPEmbedder):
        clip = VanillaClip(shared.sd_model.cond_stage_model.wrapped)
    elif isinstance(clip, FrozenOpenCLIPEmbedder):
        clip = OpenClip(shared.sd_model.cond_stage_model.wrapped)
    else:
        return f"Unknown CLIP model: {type(clip).__name__}"

    multi_prompt_schedules = get_multi_prompt_schedules(text, steps)
    # sub_texts = prompt_parser.re_AND.split(text)

    def inner() -> Generator[str, None, None]:
        nonlocal clip, multi_prompt_schedules

        for i, prompt_schedules in enumerate(multi_prompt_schedules):
            # chunked_sub_texts = prompt_parser.re_break.split(sub_texts[i])
            scheduled_chunks = get_scheduled_tokenized_chunks(prompt_schedules.schedules)
            # text_chunk_count, token_chunk_count = len(chunked_sub_texts), len(scheduled_tokenized_chunks)

            yield "".join(convert_chunks_to_html(clip, scheduled_chunks))

    row_html_class = f"i-tokenizer-row{' negative' if negative else ''}"
    return f"<div class='{row_html_class}'>{'<p>AND</p>'.join(inner())}</div>"


def tokenize_neg(text):
    return tokenize(text, negative=True)


class Script(scripts.Script):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def title(self):
        return "Interactive Tokenizer"

    def show(self, is_img2img):
        # return True
        return scripts.AlwaysVisible

    def after_component(self, component, **kwargs):
        # コンポーネントがプロンプト入力欄だった場合
        elem_id_result = re.match(r"^(txt|img)2img(|_neg)_prompt$", str(component.elem_id))
        if elem_id_result:
            component: gr.components.Textbox
            id_prefix = f"{'i2i' if self.is_txt2img else 't2i'}{elem_id_result.group(2)}"
            with (result_row := gr.Row(elem_id=f"{id_prefix}_i_tokenizer_result")):
                tokenizer_result_text = gr.HTML(elem_id=f"{id_prefix}_i_tokenizer_result_text")

            result_row.parent.children.remove(result_row)
            component.parent.parent.add(result_row)
            component.change(
                fn=tokenize_neg if elem_id_result.group(2) == "_neg" else tokenize,
                inputs=[component],
                outputs=[tokenizer_result_text]
            )
