"""Tests for template utility."""

import pytest

from cli_agent_orchestrator.utils.template import render_template


class TestRenderTemplate:
    """Tests for render_template function."""

    def test_render_single_variable(self):
        """Test rendering template with single variable."""
        template = "Hello [[name]]!"
        variables = {"name": "World"}

        result = render_template(template, variables)

        assert result == "Hello World!"

    def test_render_multiple_variables(self):
        """Test rendering template with multiple variables."""
        template = "[[greeting]] [[name]], welcome to [[place]]!"
        variables = {"greeting": "Hello", "name": "Alice", "place": "Wonderland"}

        result = render_template(template, variables)

        assert result == "Hello Alice, welcome to Wonderland!"

    def test_render_repeated_variable(self):
        """Test rendering template with same variable multiple times."""
        template = "[[name]] said hello. [[name]] waved goodbye."
        variables = {"name": "Bob"}

        result = render_template(template, variables)

        assert result == "Bob said hello. Bob waved goodbye."

    def test_render_no_variables(self):
        """Test rendering template without variables."""
        template = "No variables here"
        variables = {}

        result = render_template(template, variables)

        assert result == "No variables here"

    def test_render_extra_variables(self):
        """Test rendering with extra unused variables."""
        template = "Hello [[name]]!"
        variables = {"name": "World", "unused": "value"}

        result = render_template(template, variables)

        assert result == "Hello World!"

    def test_render_missing_variable(self):
        """Test rendering with missing variable raises error."""
        template = "Hello [[name]] and [[other]]!"
        variables = {"name": "World"}

        with pytest.raises(ValueError, match="Missing template variables: other"):
            render_template(template, variables)

    def test_render_multiple_missing_variables(self):
        """Test rendering with multiple missing variables."""
        template = "[[a]] [[b]] [[c]]"
        variables = {}

        with pytest.raises(ValueError, match="Missing template variables:"):
            render_template(template, variables)

    def test_render_numeric_value(self):
        """Test rendering with numeric variable value."""
        template = "Count: [[count]]"
        variables = {"count": 42}

        result = render_template(template, variables)

        assert result == "Count: 42"

    def test_render_empty_string_value(self):
        """Test rendering with empty string value."""
        template = "Value: [[value]]"
        variables = {"value": ""}

        result = render_template(template, variables)

        assert result == "Value: "

    def test_render_special_characters_in_value(self):
        """Test rendering with special characters in value."""
        template = "Path: [[path]]"
        variables = {"path": "/home/user/file.txt"}

        result = render_template(template, variables)

        assert result == "Path: /home/user/file.txt"

    def test_render_multiline_template(self):
        """Test rendering multiline template."""
        template = """Name: [[name]]
Age: [[age]]
City: [[city]]"""
        variables = {"name": "Alice", "age": 30, "city": "Boston"}

        result = render_template(template, variables)

        expected = """Name: Alice
Age: 30
City: Boston"""
        assert result == expected
