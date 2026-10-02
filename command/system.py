"""
pairwise-command-system - CommandSystem

This module contains a deliberately broken implementation.
Fix all bugs so that tests pass.
"""


class CommandSystem:
    def __init__(self):
        self.state = {}

    def register_command(self, *args, **kwargs):
        """BUG: 注册命令不做参数定义，不知道命令需要什么参数"""
        return None

    def parse_command(self, *args, **kwargs):
        """BUG: 命令解析不做引号处理，带空格的参数被拆开"""
        return None

    def execute_command(self, *args, **kwargs):
        """BUG: 命令执行不做返回值，执行完不知道成功失败"""
        return None

    def resolve_variable(self, *args, **kwargs):
        """BUG: 变量解析不做作用域，全局变量和局部变量混"""
        return None

    def run_script(self, *args, **kwargs):
        """BUG: 脚本执行不做控制流，不支持if和循环"""
        return None

