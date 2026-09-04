import subprocess


def close_account(account_id: str) -> None:
    command = ["accountctl", "close", account_id]
    subprocess.run(  # noqa: S602 - intentional vulnerable scanner fixture
        " ".join(command), shell=True, check=True
    )
