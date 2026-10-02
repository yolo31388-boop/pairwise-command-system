import pytest
from command.system import CommandSystem


class TestCommandSystem:

    def test_register_command(self):
        """注册命令不做参数定义，不知道命令需要什么参数"""
        obj = CommandSystem()
        result = obj.register_command()
        self.assertIsNotNone(result)

    def test_parse_command(self):
        """命令解析不做引号处理，带空格的参数被拆开"""
        obj = CommandSystem()
        result = obj.parse_command()
        self.assertIsNotNone(result)

    def test_execute_command(self):
        """命令执行不做返回值，执行完不知道成功失败"""
        obj = CommandSystem()
        result = obj.execute_command()
        self.assertIsNotNone(result)

    def test_resolve_variable(self):
        """变量解析不做作用域，全局变量和局部变量混"""
        obj = CommandSystem()
        result = obj.resolve_variable()
        self.assertIsNotNone(result)

    def test_run_script(self):
        """脚本执行不做控制流，不支持if和循环"""
        obj = CommandSystem()
        result = obj.run_script()
        self.assertIsNotNone(result)

