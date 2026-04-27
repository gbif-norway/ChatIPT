import json
import hashlib
import tempfile
from dataclasses import dataclass, field
from typing import Any, Dict, List

from django.conf import settings
from pydantic import BaseModel
from openai import OpenAI, InternalServerError
from tenacity import retry, stop_after_attempt, wait_fixed, retry_if_exception_type


@dataclass
class CompatFunctionCall:
    name: str
    arguments: str

    def dict(self):
        return {
            'name': self.name,
            'arguments': self.arguments,
        }


@dataclass
class CompatToolCall:
    id: str
    function: CompatFunctionCall
    type: str = 'function'

    def dict(self):
        return {
            'id': self.id,
            'type': self.type,
            'function': self.function.dict(),
        }


@dataclass
class CompatAssistantMessage:
    role: str = 'assistant'
    content: str = ''
    tool_calls: List[CompatToolCall] = field(default_factory=list)

    def dict(self):
        payload = {
            'role': self.role,
            'content': self.content,
        }
        if self.tool_calls:
            payload['tool_calls'] = [tool_call.dict() for tool_call in self.tool_calls]
        return payload


@retry(retry=retry_if_exception_type(InternalServerError), stop=stop_after_attempt(10), wait=wait_fixed(2))
def query_responses_api(args):
    timeout_seconds = float(getattr(settings, "OPENAI_RESPONSES_TIMEOUT_SECONDS", 180.0))
    with OpenAI(timeout=timeout_seconds) as client:
        return client.responses.create(**args)


def create_response_message(messages, functions, temperature=1, model='gpt-5.4', pdf_user_files=None):
    print('---')
    print(f'---Calling GPT {model}---')
    input_items = _messages_to_responses_input(messages)
    pdf_file_inputs = _prepare_pdf_file_inputs(pdf_user_files if pdf_user_files is not None else [])
    if pdf_file_inputs:
        input_items = _attach_pdf_files_to_latest_user_message(input_items, pdf_file_inputs)

    openai_args = {
        'model': model,
        'temperature': temperature,
        'input': input_items,
    }
    openai_args['tools'] = _functions_to_responses_tools(functions)
    response = query_responses_api(openai_args)
    print(
        '---Response'
        f' id={getattr(response, "id", "-")}'
        f' status={getattr(response, "status", "-")}'
        f' output_items={len(getattr(response, "output", []) or [])}'
        '---'
    )
    return _response_to_compat_message(response)


def _prepare_pdf_file_inputs(pdf_user_files) -> List[Dict[str, str]]:
    file_inputs = []
    for user_file in pdf_user_files:
        if not getattr(user_file, 'file', None):
            continue
        filename = getattr(user_file, 'filename', '') or 'uploaded.pdf'
        file_id = _ensure_openai_file_id(user_file)
        file_inputs.append({
            'type': 'input_file',
            'file_id': file_id,
            'filename': filename,
            'user_file_id': str(getattr(user_file, 'id', '')),
        })
    return file_inputs


def _ensure_openai_file_id(user_file) -> str:
    user_file.file.open('rb')
    try:
        file_bytes = user_file.file.read()
    finally:
        user_file.file.close()

    fingerprint = hashlib.sha256(file_bytes).hexdigest()
    if (
        getattr(user_file, 'openai_file_id', '')
        and getattr(user_file, 'openai_file_fingerprint', '') == fingerprint
    ):
        return user_file.openai_file_id

    with tempfile.NamedTemporaryFile(suffix='.pdf') as tmp:
        tmp.write(file_bytes)
        tmp.flush()
        with OpenAI() as client:
            with open(tmp.name, 'rb') as pdf_handle:
                uploaded = client.files.create(file=pdf_handle, purpose='user_data')

    user_file.openai_file_id = uploaded.id
    user_file.openai_file_fingerprint = fingerprint
    user_file.save(update_fields=['openai_file_id', 'openai_file_fingerprint'])
    return uploaded.id


def _attach_pdf_files_to_latest_user_message(
    input_items: List[Dict[str, Any]],
    pdf_file_inputs: List[Dict[str, str]],
) -> List[Dict[str, Any]]:
    if not pdf_file_inputs:
        return input_items

    file_labels = ', '.join(
        item.get('filename') or item.get('file_id') or 'uploaded PDF'
        for item in pdf_file_inputs
    )
    attachment_note = {
        'type': 'input_text',
        'text': (
            f'Uploaded manuscript PDF files are attached to this message for direct reading: {file_labels}. '
            'Use these file attachments as source evidence; do not rely on a separate PDF parser.'
        ),
    }
    attachment_content = [
        attachment_note,
        *[
            {'type': 'input_file', 'file_id': item['file_id']}
            for item in pdf_file_inputs
        ],
    ]

    for index in range(len(input_items) - 1, -1, -1):
        item = input_items[index]
        if item.get('role') != 'user':
            continue

        content = item.get('content')
        if isinstance(content, list):
            text_and_files = [*content, *attachment_content]
        else:
            text_and_files = [
                {'type': 'input_text', 'text': _normalize_content_to_text(content)},
                *attachment_content,
            ]

        updated_items = list(input_items)
        updated_items[index] = {**item, 'content': text_and_files}
        return updated_items

    return [
        *input_items,
        {
            'role': 'user',
            'content': attachment_content,
        },
    ]


def _messages_to_responses_input(messages) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for message in messages:
        openai_obj = getattr(message, 'openai_obj', None) or {}
        role = str(openai_obj.get('role') or '').strip().lower()
        content_text = _normalize_content_to_text(openai_obj.get('content'))

        if role == 'tool':
            tool_call_id = openai_obj.get('tool_call_id')
            if tool_call_id:
                items.append({
                    'type': 'function_call_output',
                    'call_id': str(tool_call_id),
                    'output': content_text,
                })
            elif content_text:
                items.append({'role': 'user', 'content': content_text})
            continue

        if role == 'assistant':
            if content_text:
                items.append({'role': 'assistant', 'content': content_text})

            for tool_call in openai_obj.get('tool_calls') or []:
                function_obj = (tool_call or {}).get('function') or {}
                tool_call_id = (tool_call or {}).get('id')
                function_name = function_obj.get('name')
                arguments = _normalize_function_arguments(function_obj.get('arguments'))
                if tool_call_id and function_name:
                    items.append({
                        'type': 'function_call',
                        'call_id': str(tool_call_id),
                        'name': str(function_name),
                        'arguments': arguments,
                    })
            continue

        # system/user (and any unexpected role fallback)
        items.append({
            'role': role if role in {'system', 'user'} else 'user',
            'content': content_text,
        })

    return items


def _functions_to_responses_tools(functions) -> List[Dict[str, Any]]:
    tools = []
    for function_model in functions:
        schema = function_model.openai_schema()
        tools.append({
            'type': 'function',
            'name': schema['name'],
            'description': schema.get('description') or '',
            'parameters': schema['parameters'],
        })
    return tools


def _response_to_compat_message(response) -> CompatAssistantMessage:
    content_parts: List[str] = []
    tool_calls: List[CompatToolCall] = []

    for item in getattr(response, 'output', []) or []:
        item_type = getattr(item, 'type', '')
        if item_type == 'message':
            for content in getattr(item, 'content', []) or []:
                content_type = getattr(content, 'type', '')
                if content_type in {'output_text', 'text'}:
                    chunk = getattr(content, 'text', '')
                    if chunk:
                        content_parts.append(str(chunk))
        elif item_type == 'function_call':
            call_id = getattr(item, 'call_id', None) or getattr(item, 'id', None)
            name = getattr(item, 'name', None)
            if not call_id or not name:
                continue
            arguments = _normalize_function_arguments(getattr(item, 'arguments', ''))
            tool_calls.append(
                CompatToolCall(
                    id=str(call_id),
                    function=CompatFunctionCall(
                        name=str(name),
                        arguments=arguments,
                    ),
                )
            )

    content = '\n'.join(content_parts).strip()
    if not content and getattr(response, 'output_text', None):
        content = str(response.output_text).strip()

    return CompatAssistantMessage(content=content, tool_calls=tool_calls)


def _normalize_content_to_text(content: Any) -> str:
    if content is None:
        return ''
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for entry in content:
            if isinstance(entry, str):
                parts.append(entry)
                continue
            if isinstance(entry, dict):
                text = entry.get('text')
                if isinstance(text, str):
                    parts.append(text)
                    continue
            parts.append(json.dumps(entry, ensure_ascii=False))
        return '\n'.join(parts)
    if isinstance(content, dict):
        text = content.get('text')
        if isinstance(text, str):
            return text
        return json.dumps(content, ensure_ascii=False)
    return str(content)


def _normalize_function_arguments(arguments: Any) -> str:
    if isinstance(arguments, str):
        return arguments
    try:
        return json.dumps(arguments or {}, ensure_ascii=False)
    except Exception:
        return str(arguments)

def custom_schema(cls: BaseModel) -> Dict[str, Any]:
    parameters = cls.schema()
    # Remove internal Pydantic helper fields we never want to expose
    parameters['properties'] = {
        k: v
        for k, v in parameters['properties'].items()
        if k not in ('v__duplicate_kwargs', 'args', 'kwargs')
    }

    # Pydantic already sets the correct list of required fields in the schema.  
    # Don't overwrite it with *all* properties (that turned every optional field into a required one).
    # We only ensure the key exists so OpenAI gets a well-formed JSON Schema.
    parameters['required'] = parameters.get('required', [])

    # Clean out noisy schema metadata keys while preserving actual field names
    # in maps like `properties` (e.g., a real field named "description").
    _remove_schema_metadata_noise(parameters)
    return {
        'name': cls.__name__,
        'description': cls.__doc__,
        'parameters': parameters,
    }


class OpenAIBaseModel(BaseModel):
    @classmethod
    def openai_schema(cls):
        return custom_schema(cls)

# def get_function(fn):
#     if fn.name.lower() == 'python' and fn.arguments.replace(' ', '')[:8] != '{"code":':
#         print('Python args not wrapped in code')
#         fn.arguments = {'code': fn.arguments}
#     else:
#         fn.arguments = json.loads(fn.arguments, strict=False) 
#     return fn

def _remove_schema_metadata_noise(node, preserve_map_keys: bool = False) -> None:
    """Remove verbose schema metadata without deleting actual property names."""
    if isinstance(node, dict):
        for key in list(node.keys()):
            value = node[key]

            # In mapping containers, keys are user-defined identifiers (e.g. field names).
            # Preserve those keys even if they match metadata names like "title".
            if preserve_map_keys:
                _remove_schema_metadata_noise(value, preserve_map_keys=False)
                continue

            if key in {"title", "description", "additionalProperties"}:
                del node[key]
                continue

            child_preserve_map_keys = key in {"properties", "$defs", "definitions", "patternProperties"}
            _remove_schema_metadata_noise(value, preserve_map_keys=child_preserve_map_keys)
    elif isinstance(node, list):
        for item in node:
            _remove_schema_metadata_noise(item, preserve_map_keys=False)
