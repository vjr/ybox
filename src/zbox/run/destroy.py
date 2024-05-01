import argparse
import sys

from zbox.cmd import get_docker_command, run_command, verify_zbox_state
from zbox.env import Environ
from zbox.print import fgcolor, print_color, print_error, print_warn
from zbox.state import ZboxStateManagement


def main() -> None:
    main_argv(sys.argv[1:])


def main_argv(argv: list[str]) -> None:
    args = parse_args(argv)
    docker_cmd = get_docker_command(args, "-d")
    container_name = args.container_name

    verify_zbox_state(docker_cmd, container_name, expected_states=[])
    print_color(f"Stopping zbox container '{container_name}'", fg=fgcolor.cyan)
    # continue even if this fails since the container may already be in stopped state
    run_command([docker_cmd, "container", "stop", container_name],
                exit_on_error=False, error_msg=f"stopping '{container_name}'")

    print_warn(f"Removing zbox container '{container_name}'")
    rm_args = [docker_cmd, "container", "rm"]
    if args.force:
        rm_args.append("--force")
    rm_args.append(container_name)
    run_command(rm_args, error_msg=f"removing '{container_name}'")

    # remove the state from the database
    print_warn(f"Clearing zbox state for '{container_name}'")
    with ZboxStateManagement(Environ()) as state:
        if not state.unregister_container(container_name):
            print_error(f"No entry found for '{container_name}' in the state database")
            sys.exit(1)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stop and remove a running zbox container")
    parser.add_argument("-d", "--docker-path", type=str,
                        help="path of docker/podman if not in /usr/bin")
    parser.add_argument("-f", "--force", action="store_true",
                        help="force destroy the container using SIGKILL if required")
    parser.add_argument("container_name", type=str, help="name of the running zbox")
    return parser.parse_args(argv)