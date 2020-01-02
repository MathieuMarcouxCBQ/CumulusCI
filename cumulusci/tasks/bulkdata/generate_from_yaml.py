import os
from typing import TextIO, Optional
from pathlib import Path
import shutil

import yaml


from cumulusci.core.utils import process_list_of_pairs_dict_arg

from cumulusci.core.exceptions import TaskOptionsError
from cumulusci.tasks.bulkdata.base_generate_data_task import BaseGenerateDataTask
from snowfakery.output_streams import SqlOutputStream
from snowfakery.data_generator import generate, StoppingCriteria
from snowfakery.generate_mapping_from_factory import mapping_from_factory_templates


class GenerateDataFromYaml(BaseGenerateDataTask):
    """Generate sample data from a YAML template file."""

    task_docs = """
    Depends on the currently un-released Snowfakery tool from SFDO. Better documentation
    will appear here when Snowfakery is publically available.
    """

    task_options = {
        **BaseGenerateDataTask.task_options,
        "generator_yaml": {
            "description": "A generator YAML file to use",
            "required": True,
        },
        "vars": {
            "description": "Pass values to override options in the format VAR1:foo,VAR2:bar"
        },
        "generate_mapping_file": {
            "description": "A path to put a mapping file inferred from the generator_yaml",
            "required": False,
        },
        "num_records_tablename": {
            "description": "A string representing which table to count records in."
        },
        "continuation_file": {
            "description": "YAML file for Snowfakery representing next steps for data generation"
        },
        "generate_continuation_file": {
            "description": "Path to put the next steps for data generation"
        },
        "working_directory": {
            "description": "Default path for temporary / working files"
        },
    }
    stopping_criteria = None

    def __init__(self, *args, **kwargs):
        self.vars = {}
        super().__init__(*args, **kwargs)

    def _init_options(self, kwargs):
        super()._init_options(kwargs)
        self.yaml_file = os.path.abspath(self.options["generator_yaml"])
        if not os.path.exists(self.yaml_file):
            raise TaskOptionsError(f"Cannot find {self.yaml_file}")
        if "vars" in self.options:
            self.vars = process_list_of_pairs_dict_arg(self.options["vars"])
        self.generate_mapping_file = self.options.get("generate_mapping_file")
        if self.generate_mapping_file:
            self.generate_mapping_file = os.path.abspath(self.generate_mapping_file)
        num_records = self.options.get("num_records")
        if num_records is not None:
            num_records_tablename = self.options.get("num_records_tablename")

            if not num_records_tablename:
                raise TaskOptionsError(
                    f"Cannot specify num_records without num_records_tablename."
                )

            self.stopping_criteria = StoppingCriteria(
                num_records_tablename, num_records
            )
        self.working_directory = self.options.get("working_directory")

    def _generate_data(self, db_url, mapping_file_path, num_records, current_batch_num):
        """Generate all of the data"""
        if mapping_file_path:
            with open(mapping_file_path, "r") as f:
                self.mappings = yaml.safe_load(f)
        else:
            self.mappings = {}
        session, engine, base = self.init_db(db_url, self.mappings)
        self.logger.info(f"Generating batch {current_batch_num} with {num_records}")
        self.generate_data(session, engine, base, num_records, current_batch_num)
        session.commit()
        session.close()

    def default_continuation_file_path(self):
        return Path(self.working_directory) / "continuation.yml"

    def get_old_continuation_file(self) -> Optional[Path]:
        """Use a continuation file if specified or look for one in the working directory

        Return None if no file can be found.
        """
        old_continuation_file = self.options.get("continuation_file")

        if old_continuation_file:
            old_continuation_file = Path(old_continuation_file)
            if not old_continuation_file.exists():
                raise TaskOptionsError(f"{old_continuation_file} does not exit")
        elif self.working_directory:
            path = self.default_continuation_file_path()
            if path.exists():
                old_continuation_file = path

        return old_continuation_file

    def open_new_continuation_file(self) -> Optional[TextIO]:
        """Create a continuation file based on config or working directory

        Return None if there is no config nor working directory.
        """
        if self.options.get("generate_continuation_file"):
            new_continuation_file = open(
                self.options["generate_continuation_file"], "w+"
            )
        elif self.working_directory:
            new_continuation_file = open(
                Path(self.working_directory) / "continuation_next.yml", "w+"
            )
        else:
            new_continuation_file = None
        return new_continuation_file

    def generate_data(self, session, engine, base, num_records, current_batch_num):
        output_stream = SqlOutputStream.from_connection(
            session, engine, base, self.mappings
        )
        old_continuation_file = self.get_old_continuation_file()
        if old_continuation_file:
            # reopen to ensure file pointer is at starting point
            old_continuation_file = open(old_continuation_file, "r")
        new_continuation_file = self.open_new_continuation_file()

        with open(self.yaml_file) as open_yaml_file:
            summary = generate(
                open_yaml_file=open_yaml_file,
                user_options=self.vars,
                output_stream=output_stream,
                stopping_criteria=self.stopping_criteria,
                continuation_file=old_continuation_file,
                generate_continuation_file=new_continuation_file,
            )
            output_stream.close()

            if (
                new_continuation_file
                and Path(new_continuation_file.name).exists()
                and self.working_directory
            ):
                shutil.copyfile(
                    new_continuation_file.name, self.default_continuation_file_path()
                )

            if self.generate_mapping_file:
                with open(self.generate_mapping_file, "w+") as f:
                    yaml.safe_dump(
                        mapping_from_factory_templates(summary), f, sort_keys=False
                    )
                    f.close()
