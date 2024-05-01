import argparse
import sys

from zbox.cmd import get_docker_command, run_command


def main() -> None:
    main_argv(sys.argv[1:])


def main_argv(argv: list[str]) -> None:
    args = parse_args(argv)
    docker_cmd = get_docker_command(args, "-d")
    container_name = args.container_name

    # verify_zbox_state(docker_cmd, container_name, ["running"], error_msg=" active ")

    run_command([docker_cmd, "exec", "-it", container_name, args.shell],
                error_msg=f"{args.shell} execution on '{container_name}'")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Open a shell on a running zbox container")
    parser.add_argument("-d", "--docker-path", type=str,
                        help="path of docker/podman if not in /usr/bin")
    parser.add_argument("-s", "--shell", type=str, default="/bin/bash",
                        help="run the given shell (default is /bin/bash)")
    parser.add_argument("container_name", type=str, help="name of the running zbox")
    return parser.parse_args(argv)
