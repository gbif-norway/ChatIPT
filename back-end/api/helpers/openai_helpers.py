import ast
from pydantic import BaseModel
import json
from openai import OpenAI, InternalServerError
from pprint import pprint
from typing import Dict, Any
from tenacity import retry, stop_after_attempt, wait_fixed, retry_if_exception_type

@retry(retry=retry_if_exception_type(InternalServerError), stop=stop_after_attempt(10), wait=wait_fixed(2))
def query_api(args):
    with OpenAI() as client:
        return client.chat.completions.create(**args)  

def create_chat_completion(messages, functions=None, temperature=0.7, model='gpt-4o-2024-05-13'): # gpt-4o-2024-08-06
    print('---')
    print(f'---Calling GPT {model}---')
    openai_args = { 'model': model, 'temperature': temperature, 'messages': [m.openai_obj for m in messages] }
    if functions:
        openai_args['tools'] = [{'type': 'function', 'function': f.openai_schema()} for f in functions]
    print(openai_args)
    response = query_api(openai_args)
    pprint(f'---Response---\n{response}\n---')
    return response.choices[0].message

def custom_schema(cls: BaseModel) -> Dict[str, Any]:
    parameters = cls.schema()
    parameters['properties'] = {
        k: v
        for k, v in parameters['properties'].items()
        if k not in ('v__duplicate_kwargs', 'args', 'kwargs')
    }
    parameters['required'] = sorted(parameters['properties'])
    for key in ['title', 'additionalProperties', 'description']:
         _remove_a_key(key, 'title')
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

def _remove_a_key(d, remove_key) -> None:
    """Remove a key from a dictionary recursively"""
    if isinstance(d, dict):
        for key in list(d.keys()):
            if key == remove_key:
                del d[key]
            else:
                _remove_a_key(d[key], remove_key)
