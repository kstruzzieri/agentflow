import json
import subprocess
import sys

request = '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'
result = subprocess.run([sys.executable, "-m", "agentflow.mcp_server"], input=request, text=True, stdout=subprocess.PIPE, check=True)
print(result.stdout.strip())
