# Copyright (c) 2023, NVIDIA CORPORATION.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import typing
from datetime import datetime

import click
# When segment modules are imported, they're added to the module registry.
# To avoid flake8 warnings about unused code, the noqa flag is used during import.
import dfp.modules  # noqa: F401 # pylint:disable=unused-import
from dfp.utils.config_generator import ConfigGenerator
from dfp.utils.config_generator import generate_ae_config
from dfp.utils.dfp_arg_parser import DFPArgParser
from dfp.utils.schema_utils import Schema
from dfp.utils.schema_utils import SchemaBuilder

import morpheus.loaders  # noqa: F401 # pylint:disable=unused-import
import morpheus.modules  # noqa: F401 # pylint:disable=unused-import
from morpheus.cli.utils import get_log_levels
from morpheus.cli.utils import parse_log_level
from morpheus.config import Config
from morpheus.pipeline.pipeline import Pipeline
from morpheus.stages.general.multi_port_modules_stage import MultiPortModulesStage
from morpheus.stages.input.control_message_kafka_source_stage import ControlMessageKafkaSourceStage


@click.command()
@click.option(
    "--source",
    type=click.Choice(["duo", "azure"], case_sensitive=False),
    required=True,
    help=("Indicates what type of logs are going to be used in the workload."),
)
@click.option(
    "--train_users",
    type=click.Choice(["all", "generic", "individual"], case_sensitive=False),
    help=("Indicates whether or not to train per user or a generic model for all users. "
          "Selecting none runs the inference pipeline."),
)
@click.option(
    "--skip_user",
    multiple=True,
    type=str,
    help="User IDs to skip. Mutually exclusive with only_user",
)
@click.option(
    "--only_user",
    multiple=True,
    type=str,
    help="Only users specified by this option will be included. Mutually exclusive with skip_user",
)
@click.option(
    "--start_time",
    type=click.DateTime(
        formats=['%Y-%m-%d', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S%z', '%Y-%m-%d %H:%M:%S%z']),
    default=None,
    help="The start of the time window, if undefined start_date will be `now()-duration`",
)
@click.option(
    "--duration",
    type=str,
    default="60d",
    help="The training duration to run starting from start_time",
)
@click.option(
    "--use_cpp",
    type=click.BOOL,
    default=True,
    help=("Indicates what type of logs are going to be used in the workload."),
)
@click.option(
    "--cache_dir",
    type=str,
    default="./.cache/dfp",
    show_envvar=True,
    help="The location to cache data such as S3 downloads and pre-processed data",
)
@click.option("--log_level",
              default=logging.getLevelName(Config().log_level),
              type=click.Choice(get_log_levels(), case_sensitive=False),
              callback=parse_log_level,
              help="Specify the logging level to use.")
@click.option("--sample_rate_s",
              type=int,
              default=0,
              show_envvar=True,
              help="Minimum time step, in milliseconds, between object logs.")
@click.option('--tracking_uri',
              type=str,
              default="http://mlflow:5000",
              help=("The MLflow tracking URI to connect to the tracking backend."))
@click.option('--bootstrap_servers',
              type=str,
              default="localhost:9092",
              required=True,
              help=("Comma-separated list of bootstrap servers."))
@click.option('--input_topic',
              type=str,
              default=["test_cm"],
              multiple=True,
              help="Topic(s) to consume message(s) from.")
@click.option('--group_id', type=str, default="morpheus", required=True, help="")
@click.option('--poll_interval',
              type=str,
              default="10millis",
              required=True,
              help="Polling interval to check for messages.")
@click.option("--disable_commit",
              is_flag=False,
              help=("Enabling this option will skip committing messages as they are pulled off the server. "
                    "This is only useful for debugging, allowing the user to process the same messages multiple times"))
@click.option("--disable_pre_filtering",
              is_flag=True,
              help=("Enabling this option will skip pre-filtering of json messages. "
                    "This is only useful when inputs are known to be valid json."))
@click.option('--silence_monitors', flag_value=True, help='Controls whether monitors will be verbose.')
def run_pipeline(source: str,
                 train_users: str,
                 skip_user: typing.Tuple[str],
                 only_user: typing.Tuple[str],
                 start_time: datetime,
                 duration: str,
                 cache_dir: str,
                 log_level: int,
                 sample_rate_s: int,
                 tracking_uri,
                 silence_monitors,
                 use_cpp,
                 **kwargs):
    if (skip_user and only_user):
        logging.error("Option --skip_user and --only_user are mutually exclusive. Exiting")

    dfp_arg_parser = DFPArgParser(skip_user,
                                  only_user,
                                  start_time,
                                  log_level,
                                  cache_dir,
                                  sample_rate_s,
                                  duration,
                                  source,
                                  tracking_uri,
                                  silence_monitors,
                                  train_users)

    dfp_arg_parser.init()

    # Default user_id column -- override with ControlMessage
    userid_column_name = "username"
    # Default timestamp column -- override with ControlMessage
    timestamp_column_name = "timestamp"

    config: Config = generate_ae_config(source, userid_column_name, timestamp_column_name, use_cpp=use_cpp)

    # Construct the data frame Schema used to normalize incoming data
    schema_builder = SchemaBuilder(config, source)
    schema: Schema = schema_builder.build_schema()

    # Create config helper used to generate config parameters for the DFP module
    # This will populate to the minimum configuration parameters with intelligent default values
    config_generator = ConfigGenerator(config, dfp_arg_parser, schema)

    dfp_deployment_module_config = config_generator.get_module_conf()

    #                                     +--------------------------------------+
    #                                     |  control_message_kafka_source_stage  |
    #                                     +--------------------------------------+
    #                                                        |
    #                                                        v
    #                                          +------------------------+
    #                                          |  dfp_deployment_module |
    # +-------------------------------------------------------------------------------------------------------------+
    # |                                                      |                                                      |
    # |                                                      v                                                      |
    # |                                   +-------------------------------------+                                   |
    # |                                   |       fsspec_loader_module          |                                   |
    # |                                   +-------------------------------------+                                   |
    # |                                                      |                                                      |
    # |                                                      v                                                      |
    # |                                   +-------------------------------------+                                   |
    # |                                   |              broadcast              |                                   |
    # |                                   +-------------------------------------+                                   |
    # |                                              /                \                                             |
    # |                                             /                  \                                            |
    # |                                            /                    \                                           |
    # |                                           v                      v                                          |
    # |                          +-------------------------+    +-------------------------+                         |
    # |                          |dfp_trianing_pipe_module |    |dfp_inference_pipe_module|                         |
    # |   +------------------------------------------------+    +----------------------------------------------+    |
    # |   |                                                |    |                                               |   |
    # |   |      +-------------------------------------+   |    |    + -------------------------------------+   |   |
    # |   |      |           preproc_module            |   |    |    |           preproc_module             |   |   |
    # |   |      +-------------------------------------+   |    |    + -------------------------------------+   |   |
    # |   |                        |                       |    |                        |                      |   |
    # |   |                        v                       |    |                        v                      |   |
    # |   |      +-------------------------------------+   |    |    + -------------------------------------+   |   |
    # |   |      |     dfp_rolling_window_module       |   |    |    |      dfp_rolling_window_module      |    |   |
    # |   |      +-------------------------------------+   |    |    + -------------------------------------+   |   |
    # |   |                        |                       |    |                        |                      |   |
    # |   |                        v                       |    |                        v                      |   |
    # |   |      +-------------------------------------+   |    |    + -------------------------------------+   |   |
    # |   |      |        dfp_data_prep_module         |   |    |    |         dfp_data_prep_module         |   |   |
    # |   |      +-------------------------------------+   |    |    + -------------------------------------+   |   |
    # |   |                        |                       |    |                        |                      |   |
    # |   |                        v                       |    |                        v                      |   |
    # |   |      +-------------------------------------+   |    |    + -------------------------------------+   |   |
    # |   |      |         dfp_monitor_module          |   |    |    |           dfp_monitor_module         |   |   |
    # |   |      +-------------------------------------+   |    |    + -------------------------------------+   |   |
    # |   |                        |                       |    |                        |                      |   |
    # |   |                        v                       |    |                        v                      |   |
    # |   |      +-------------------------------------+   |    |    + -------------------------------------+   |   |
    # |   |      |        dfp_training_module          |   |    |    |         dfp_inference_module         |   |   |
    # |   |      +-------------------------------------+   |    |    + -------------------------------------+   |   |
    # |   |                        |                       |    |                        |                      |   |
    # |   |                        v                       |    |                        v                      |   |
    # |   |      +-------------------------------------+   |    |    + -------------------------------------+   |   |
    # |   |      |         dfp_monitor_module          |   |    |    |           dfp_monitor_module        |    |   |
    # |   |      +-------------------------------------+   |    |    + -------------------------------------+   |   |
    # |   |                        |                       |    |                        |                      |   |
    # |   |                        v                       |    |                        v                      |   |
    # |   |      +-------------------------------------+   |    |    + -------------------------------------+   |   |
    # |   |      |       mlflow_model_writer_module    |   |    |    |        filter_detections_module      |   |   |
    # |   |      +-------------------------------------+   |    |    + -------------------------------------+   |   |
    # |   |                        |                       |    |                        |                      |   |
    # |   |                        v                       |    |                        v                      |   |
    # |   |      +-------------------------------------+   |    |    + -------------------------------------+   |   |
    # |   |      |          dfp_monitor_module         |   |    |    |        dfp_post_proc_module          |   |   |
    # |   |      +-------------------------------------+   |    |    + -------------------------------------+   |   |
    # |    ------------------------------------------------     |                        |                      |   |
    # |                                                         |                        v                      |   |
    # |                                                         |    +-------------------------------------+    |   |
    # |                                                         |    |          serialize_module           |    |   |
    # |                                                         |    +-------------------------------------+    |   |
    # |                                                         |                        |                      |   |
    # |                                                         |                        v                      |   |
    # |                                                         |    +-------------------------------------+    |   |
    # |                                                         |    |      write_to_file_module           |    |   |
    # |                                                         |    +-------------------------------------+    |   |
    # |                                                         |                        |                      |   |
    # |                                                         |                        v                      |   |
    # |                                                         |    +-------------------------------------+    |   |
    # |                                                         |    |          dfp_monitor_module         |    |   |
    # |                                                         |    +-------------------------------------+    |   |
    # |                                                         ------------------------------------------------    |
    #  --------------------------------------------------------------------------------------------------------------

    # Create a pipeline object
    pipeline = Pipeline(config)

    source_stage = pipeline.add_stage(
        ControlMessageKafkaSourceStage(config,
                                       bootstrap_servers=kwargs["bootstrap_servers"],
                                       input_topic=list(kwargs["input_topic"]),
                                       group_id=kwargs["group_id"],
                                       poll_interval=kwargs["poll_interval"],
                                       disable_commit=kwargs["disable_commit"],
                                       disable_pre_filtering=kwargs["disable_pre_filtering"]))

    dfp_deployment_stage = pipeline.add_stage(
        MultiPortModulesStage(config,
                              dfp_deployment_module_config,
                              input_ports=["input"],
                              output_ports=["output_0", "output_1"]))

    pipeline.add_edge(source_stage, dfp_deployment_stage)

    # Run the pipeline
    pipeline.run()


if __name__ == "__main__":
    run_pipeline(obj={}, auto_envvar_prefix='DFP', show_default=True, prog_name="dfp")
