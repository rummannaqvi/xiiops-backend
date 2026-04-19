import subprocess
import os

def run_command(command, cwd, env=None):
    """Runs a shell command and yields output line by line."""
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1
    )
    for line in process.stdout:
        yield line.strip()

    process.wait()
    if process.returncode != 0:
        yield f"Error: Command failed with exit code {process.returncode}"

def write_terraform_file(repo_path, content):
    tf_path = os.path.join(repo_path, "main.tf")
    with open(tf_path, "w") as f:
        f.write(content)

def run_terraform_command(repo_path, command, env_vars):
    """Runs generic terraform commands like 'init' or 'apply'."""
    cmd = ["terraform", command]
    if command == "apply":
        cmd.append("-auto-approve")
    return run_command(cmd, cwd=repo_path, env=env_vars)

def run_terraform_destroy(repo_path: str, env_vars: dict):
    """Specific function to destroy infrastructure."""
    for _ in run_command(["terraform", "init"], cwd=repo_path, env=env_vars):
        pass
    return run_command(["terraform", "destroy", "-auto-approve"], cwd=repo_path, env=env_vars)
