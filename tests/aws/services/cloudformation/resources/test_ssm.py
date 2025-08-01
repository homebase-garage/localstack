import os.path

import botocore.exceptions
import pytest
from localstack_snapshot.snapshots.transformer import SortingTransformer
from tests.aws.services.cloudformation.conftest import skip_if_v2_provider

from localstack.testing.pytest import markers
from localstack.utils.common import short_uid


@markers.aws.validated
@markers.snapshot.skip_snapshot_verify(paths=["$..Error.Message", "$..message"])
def test_parameter_defaults(deploy_cfn_template, aws_client, snapshot):
    ssm_parameter_value = f"custom-{short_uid()}"

    stack = deploy_cfn_template(
        template_path=os.path.join(
            os.path.dirname(__file__), "../../../templates/ssm_parameter_defaultname.yaml"
        ),
        parameters={"Input": ssm_parameter_value},
    )

    parameter_name = stack.outputs["CustomParameterOutput"]
    param = aws_client.ssm.get_parameter(Name=parameter_name)
    snapshot.match("ssm_parameter", param)
    snapshot.add_transformer(snapshot.transform.key_value("Name"))
    snapshot.add_transformer(snapshot.transform.key_value("Value"))

    stack.destroy()

    with pytest.raises(botocore.exceptions.ClientError) as ctx:
        aws_client.ssm.get_parameter(Name=parameter_name)
    snapshot.match("ssm_parameter_not_found", ctx.value.response)


@markers.aws.validated
def test_update_ssm_parameters(deploy_cfn_template, aws_client):
    ssm_parameter_value = f"custom-{short_uid()}"

    stack = deploy_cfn_template(
        template_path=os.path.join(
            os.path.dirname(__file__), "../../../templates/ssm_parameter_defaultname.yaml"
        ),
        parameters={"Input": ssm_parameter_value},
    )

    ssm_parameter_value = f"new-custom-{short_uid()}"
    deploy_cfn_template(
        is_update=True,
        stack_name=stack.stack_name,
        template_path=os.path.join(
            os.path.dirname(__file__), "../../../templates/ssm_parameter_defaultname.yaml"
        ),
        parameters={"Input": ssm_parameter_value},
    )

    parameter_name = stack.outputs["CustomParameterOutput"]
    param = aws_client.ssm.get_parameter(Name=parameter_name)
    assert param["Parameter"]["Value"] == ssm_parameter_value


@markers.aws.validated
def test_update_ssm_parameter_tag(deploy_cfn_template, aws_client):
    ssm_parameter_value = f"custom-{short_uid()}"
    tag_value = f"tag-{short_uid()}"

    stack = deploy_cfn_template(
        template_path=os.path.join(
            os.path.dirname(__file__), "../../../templates/ssm_parameter_defaultname_withtags.yaml"
        ),
        parameters={
            "Input": ssm_parameter_value,
            "TagValue": tag_value,
        },
    )
    parameter_name = stack.outputs["CustomParameterOutput"]
    ssm_tags = aws_client.ssm.list_tags_for_resource(
        ResourceType="Parameter", ResourceId=parameter_name
    )["TagList"]
    tags_pre_update = {tag["Key"]: tag["Value"] for tag in ssm_tags}
    assert tags_pre_update["A"] == tag_value

    tag_value_new = f"tag-{short_uid()}"
    deploy_cfn_template(
        is_update=True,
        stack_name=stack.stack_name,
        template_path=os.path.join(
            os.path.dirname(__file__), "../../../templates/ssm_parameter_defaultname_withtags.yaml"
        ),
        parameters={
            "Input": ssm_parameter_value,
            "TagValue": tag_value_new,
        },
    )

    ssm_tags = aws_client.ssm.list_tags_for_resource(
        ResourceType="Parameter", ResourceId=parameter_name
    )["TagList"]
    tags_post_update = {tag["Key"]: tag["Value"] for tag in ssm_tags}
    assert tags_post_update["A"] == tag_value_new

    # TODO: re-enable after fixing updates in general
    # deploy_cfn_template(
    #     is_update=True,
    #     stack_name=stack.stack_name,
    #     template_path=os.path.join(
    #         os.path.dirname(__file__), "../../templates/ssm_parameter_defaultname.yaml"
    #     ),
    #     parameters={
    #         "Input": ssm_parameter_value,
    #     },
    # )
    #
    # ssm_tags = aws_client.ssm.list_tags_for_resource(ResourceType="Parameter", ResourceId=parameter_name)['TagList']
    # assert ssm_tags == []


@skip_if_v2_provider(reason="CFNV2:DescribeStackResource")
@markers.snapshot.skip_snapshot_verify(paths=["$..DriftInformation", "$..Metadata"])
@markers.aws.validated
def test_deploy_patch_baseline(deploy_cfn_template, aws_client, snapshot):
    stack = deploy_cfn_template(
        template_path=os.path.join(
            os.path.dirname(__file__), "../../../templates/ssm_patch_baseline.yml"
        ),
    )

    describe_resource = aws_client.cloudformation.describe_stack_resource(
        StackName=stack.stack_name, LogicalResourceId="myPatchBaseline"
    )["StackResourceDetail"]
    snapshot.add_transformer(snapshot.transform.cloudformation_api())
    snapshot.add_transformer(
        snapshot.transform.key_value("PhysicalResourceId", "physical_resource_id")
    )
    snapshot.match("patch_baseline", describe_resource)


@markers.aws.validated
def test_maintenance_window(deploy_cfn_template, aws_client, snapshot):
    stack = deploy_cfn_template(
        template_path=os.path.join(
            os.path.dirname(__file__), "../../../templates/ssm_maintenance_window.yml"
        ),
    )

    describe_resource = aws_client.cloudformation.describe_stack_resources(
        StackName=stack.stack_name
    )["StackResources"]
    snapshot.add_transformer(snapshot.transform.cloudformation_api())
    snapshot.add_transformer(
        snapshot.transform.key_value("PhysicalResourceId", "physical_resource_id")
    )
    snapshot.add_transformer(
        SortingTransformer("MaintenanceWindow", lambda x: x["LogicalResourceId"]), priority=-1
    )
    snapshot.match("MaintenanceWindow", describe_resource)
