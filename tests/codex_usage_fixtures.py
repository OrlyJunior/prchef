from pathlib import Path

from prchef.clients.command_client import CommandResult
from prchef.models.agent import AgentRequest

CAPTURE = """{"type":"thread.started","thread_id":"01a18f2a-bb54-75d3-b556-84be340cb06a"}
{"type":"turn.started"}
{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"ok"}}
{"type":"turn.completed","usage":{"input_tokens":17372,"cached_input_tokens":13312,"cache_write_input_tokens":0,"output_tokens":5,"reasoning_output_tokens":0}}
"""
USAGE = {
    "input_tokens": 17372,
    "cached_input_tokens": 13312,
    "cache_write_input_tokens": 0,
    "output_tokens": 5,
    "reasoning_output_tokens": 0,
}


class Commands:
    def __init__(self, output: str, exit_code: int = 0, error: Exception | None = None):
        self.output, self.exit_code, self.error = output, exit_code, error

    def run(self, *args, **kwargs):
        if self.error:
            raise self.error
        return CommandResult(
            command=[], exit_code=self.exit_code, stdout=self.output, stderr="", duration_seconds=0
        )



def request(path: Path) -> AgentRequest:
    return AgentRequest(title="test", instructions="test", workspace_path=path)
