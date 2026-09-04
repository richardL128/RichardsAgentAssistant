import subprocess


def close_account(account_id: str) -> None:
    command = ["accountctl", "close", account_id]
    subprocess.run(command, check=True)  # noqa: S603 - intentional scanner fixture
