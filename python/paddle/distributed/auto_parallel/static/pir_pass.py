# Copyright (c) 2024 PaddlePaddle Authors. All Rights Reserved.
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

import collections
import logging
import re

import paddle
from paddle import pir
from paddle.autograd.backward_utils import ValueDict
from paddle.base.framework import auto_complete_op_role
from paddle.base.log_helper import get_logger
from paddle.distributed.fleet.meta_optimizers.common import OpRole
from paddle.distributed.passes.pass_base import PassContext, new_pass

from .mix_to_dist_pass import dist_skip_op_list
from .process_group import get_process_group
from .reshard_funcs.base_reshard_func import (
    choose_reshard_func,
    copy_dist_attr_with_new_member,
    copy_op_attr_with_new_member,
)
from .reshard_funcs.reshard_func_register import register_reshard_funcs
from .utils import (
    get_pp_stage_by_pp_degree,
    get_pp_stage_by_process_mesh,
    get_sub_process_mesh_by_program,
)

_logger = get_logger(
    __name__, logging.INFO, fmt='%(asctime)s-%(levelname)s: %(message)s'
)

register_reshard_funcs()

partition_skip_op_list = [
    "builtin.combine",
    "builtin.split",
    "pd_op.pylayer",
    "cf.yield",
    "cf.tuple_push",
    "cf.tuple_pop",
    "cf.stack_create",
]
amp_ops = ["pd_op.check_finite_and_unscale_", "pd_op.update_loss_scaling_"]


def reshard_single_value(program, op, operand, attr):
    prev_var = operand.source()
    if prev_var.is_dist() and prev_var.dist_attr() != attr:
        operand_attr = attr.as_tensor_dist_attr()
        paddle.pir.set_insertion_point(op)
        with auto_complete_op_role(program, op.op_role):
            # fold reshard
            if prev_var.get_defining_op().name() == 'dist_op.reshard':
                prev_reshard = prev_var.get_defining_op()
                prev_var = prev_reshard.operand_source(0)
                if prev_var.dist_attr() == operand_attr:
                    return prev_var
                reshard_var = paddle._C_ops.reshard_v2(prev_var, operand_attr)
                return reshard_var
            # insert reshard
            reshard_var = paddle._C_ops.reshard_v2(prev_var, operand_attr)
            return reshard_var
    return prev_var


def reshard_combine_value(program, op, operand, attr):
    prev_var = operand.source()

    assert (
        prev_var.get_defining_op().name() == 'builtin.combine'
    ), "TensorList must be defined by builtin.combine op."

    combine_op = prev_var.get_defining_op()
    array_attr = attr.as_array_attr()

    assert len(combine_op.operands()) == len(
        array_attr
    ), "The number of combine op operands and the number of dist array_attr are not equal in op"

    reshard_vars = []
    for inner_operand, inner_attr in zip(combine_op.operands(), array_attr):
        reshard_vars.append(
            reshard_single_value(program, op, inner_operand, inner_attr)
        )

    paddle.pir.set_insertion_point(op)
    with auto_complete_op_role(program, op.op_role):
        combine_value = paddle._C_ops.builtin_combine(reshard_vars)
    return combine_value


def apply_partition_pass(program):
    for op in program.global_block().ops:
        if op.name() in partition_skip_op_list:
            continue

        assert len(op.operands()) == len(
            op.dist_attr.operands()
        ), f"The number of operands and the number of op_dist_attr's operands are not equal in op: {op}"
        assert len(op.results()) == len(
            op.dist_attr.results()
        ), f"The number of results and the number of op_dist_attr's results are not equal in op: {op}"

        # deal with inplace value
        for out_idx, in_idx in paddle.core.pir.get_op_inplace_info(op).items():
            ref_op_role = op.op_role

            operand = op.operand(in_idx)
            operand_attr = op.dist_attr.operand(in_idx)
            prev_var = operand.source()
            if not prev_var.is_dist() or operand_attr == prev_var.dist_attr():
                continue

            assert (
                not prev_var.is_combine()
            ), f"The current partition pass not support inplace value of {op} is tensor list."

            operand_attr = operand_attr.as_tensor_dist_attr()

            # reshard input
            paddle.pir.set_insertion_point(op)
            with auto_complete_op_role(program, ref_op_role):
                reshard_var = paddle._C_ops.reshard_v2(prev_var, operand_attr)
                operand.set_source(reshard_var)

            result = op.result(out_idx)
            result_attr = op.dist_attr.result(out_idx).as_tensor_dist_attr()
            assert (
                operand_attr == result_attr
            ), f"For inplace value, The operend dist attr should be equal to result dist attr , please check your infer_spmd func of {op}"

            # reshard output
            paddle.pir.set_insertion_point_after(op)
            old_dist_attr = result.dist_attr()
            result.update_dist_attr(result_attr)

            with auto_complete_op_role(program, ref_op_role):
                # reshard output to assign out input
                reshard_var_1 = paddle._C_ops.reshard_v2(
                    result, prev_var.dist_attr()
                )
                paddle.assign(reshard_var_1, prev_var)

            if old_dist_attr == result.dist_attr():
                continue

            reshard_var_2 = reshard_var_1
            if old_dist_attr != reshard_var_1.dist_attr():
                with auto_complete_op_role(program, ref_op_role):
                    reshard_var_2 = paddle._C_ops.reshard_v2(
                        result, old_dist_attr
                    )

            result.replace_all_uses_with(reshard_var_1)
            reshard_var_1.get_defining_op().operand(0).set_source(result)
            reshard_var_2.get_defining_op().operand(0).set_source(result)

        for operand, attr in zip(op.operands(), op.dist_attr.operands()):
            prev_var = operand.source()
            if prev_var.is_combine():
                operand.set_source(
                    reshard_combine_value(program, op, operand, attr)
                )
            else:
                operand.set_source(
                    reshard_single_value(program, op, operand, attr)
                )
            prev_op = prev_var.get_defining_op()
            if prev_op and prev_op.num_results() == 1 and prev_var.use_empty():
                prev_op.erase()

        for var, attr in zip(op.results(), op.dist_attr.results()):
            if var.initialized() and var.is_dist() and var.dist_attr() != attr:
                paddle.pir.set_insertion_point_after(op)
                old_dist_attr = var.dist_attr()
                var.update_dist_attr(attr.as_tensor_dist_attr())

                # insert reshard
                with auto_complete_op_role(program, op.op_role):
                    reshard_var = paddle._C_ops.reshard_v2(var, old_dist_attr)
                    var.replace_all_uses_with(reshard_var)
                    reshard_var.get_defining_op().operand(0).set_source(var)
                    var.get_defining_op().set_bool_attr(
                        "replace_all_uses_with_reshard_var", True
                    )


class ReshardPasses:
    @staticmethod
    def fold_reshard_pass(dist_program):
        del_ops = []
        value_dict = ValueDict()
        for op in dist_program.global_block().ops:
            if op.name() != 'dist_op.reshard':
                continue
            input = op.operand_source(0)
            result = op.result(0)
            if input.type() == result.type():
                result.replace_all_uses_with(input)
                del_ops.append(op)
                continue
            if input not in value_dict:
                value_dict[input] = [(result.type(), result)]
                continue
            no_find = True
            for type, val in value_dict[input]:
                if type == result.type():
                    result.replace_all_uses_with(val)
                    del_ops.append(op)
                    no_find = False
                    break
            if no_find:
                value_dict[input].append((result.type(), result))
        for op in del_ops:
            op.erase()

    @staticmethod
    def reshard_op_pass(dist_program, params_grads=[]):
        # {grad.id: grad}
        sharded_grad = {}
        grad_ids = [grad.id for _, grad in params_grads if grad is not None]

        for op in dist_program.global_block().ops:
            if op.name() == 'dist_op.reshard':
                var = op.operand_source(0)

                op_dist_attr = op.dist_attr
                src_dist_attr = op_dist_attr.operand(0).as_tensor_dist_attr()
                dst_dist_attr = op_dist_attr.result(0).as_tensor_dist_attr()

                assert (
                    not var.initialized() or var.dist_attr() == src_dist_attr
                ), f"The dist_attr of reshard op's input and operand should be equal, but got {var.dist_attr()} and {src_dist_attr}"

                if src_dist_attr == dst_dist_attr:
                    op.result(0).replace_all_uses_with(var)
                    op.erase()
                    continue

                reshard_func = choose_reshard_func(src_dist_attr, dst_dist_attr)
                assert (
                    reshard_func is not None
                ), f'There is no reshard function that matches src_dist_attr: {src_dist_attr} and dst_dist_attr: {dst_dist_attr}, {var.get_defining_op()}'

                paddle.pir.set_insertion_point(op)
                ref_op_role = op.op_role

                with auto_complete_op_role(dist_program, ref_op_role):
                    out_value = reshard_func.reshard(
                        src_dist_attr,
                        dst_dist_attr,
                        op.operand_source(0),
                        op.result(0).type(),
                    )

                if out_value is not None:
                    op.result(0).replace_all_uses_with(out_value)
                    if var.id in grad_ids:
                        if var.get_defining_op().has_attr(
                            "replace_all_uses_with_reshard_var"
                        ):
                            sharded_grad[var.id] = out_value

                if op.result(0).use_empty():
                    op.erase()

                if out_value is not None and var.use_empty():
                    if var.id in grad_ids:
                        sharded_grad[var.id] = out_value

        # update params_grads with sharded grad
        for idx, (param, grad) in enumerate(params_grads):
            if grad is None:
                continue

            if grad.id in sharded_grad:
                params_grads[idx] = (param, sharded_grad[grad.id])

    @staticmethod
    def apply_reshard_pass(dist_program, params_grads=[]):
        ReshardPasses.fold_reshard_pass(dist_program)
        ReshardPasses.reshard_op_pass(dist_program, params_grads)


# Replace the specific MoE-related dist op with the
# executable op in the dense program. In expert parallelism
# of the MoE model, the process mesh of each expert is
# different. Two specific apis are used to transform the
# input tensor's global process mesh to the experts' local
# process meshes, which will add two dist ops in the program.
# The following two functions are used to replace the two
# dist ops with the executable share_data_ ops.
def replace_moe_sub_mesh_tensors(op):
    cur_rank = paddle.distributed.get_rank()
    in_value = op.operand_source(0)
    out_value = None
    out_idx = -1
    for idx, val in enumerate(op.results()):
        val_mesh = val.dist_attr().process_mesh
        if cur_rank in val_mesh.process_ids:
            assert (
                out_value is None
            ), f'{op} has more than one results on rank {cur_rank}'
            out_value = val
            out_idx = idx

    paddle.pir.set_insertion_point(op)
    local_value = paddle._C_ops.share_data_(in_value)
    local_value_type = paddle.base.libpaddle.pir.cvt_to_dist_type(
        out_value.type(), out_value.dist_attr()
    )
    local_value.set_type(local_value_type)
    out_value.replace_all_uses_with(local_value)

    op_dist_attr = op.dist_attr
    share_data_op = local_value.get_defining_op()
    share_data_op.dist_attr = (
        paddle.base.libpaddle.pir.create_op_dist_attribute(
            op_dist_attr.process_mesh,
            [op_dist_attr.operand(0).as_tensor_dist_attr()],
            [op_dist_attr.result(out_idx).as_tensor_dist_attr()],
        )
    )

    assert all(val.use_empty() for val in op.results())
    op.erase()


class RemovePasses:
    @staticmethod
    def remove_other_rank_op_pass(dist_program):
        # pruning op and value not belong to cur rank
        cur_rank = paddle.distributed.get_rank()

        for op in dist_program.global_block().ops[::-1]:
            if op.name() in partition_skip_op_list:
                can_delete = True
                for val in op.results():
                    if not val.use_empty():
                        can_delete = False
                if can_delete:
                    op.erase()
                continue
            if op.name() == "dist_op.moe_sub_mesh_tensors":
                replace_moe_sub_mesh_tensors(op)
                continue
            if op.name() == "dist_op.moe_global_mesh_tensor":
                replace_moe_global_mesh_tensor(op)
                continue
            if cur_rank not in op.dist_attr.process_mesh.process_ids:
                op.erase()
            elif op.name() == "dist_op.reshard":
                assert op.result(
                    0
                ).use_empty(), f'There should not have useful dist.reshard op in remove_other_rank_op_pass. but find : {op}'
                op.erase()

        # merge pd.data ops for
        lr_ops = []
        lr_parameters = []
        for op in dist_program.global_block().ops[::-1]:
            if (
                op.name() == 'pd_op.data'
                and "learning_rate" in op.attrs()["name"]
            ):
                lr_ops.append(op)
            if (
                op.name() == 'builtin.parameter'
                and "learning_rate" in op.attrs()["parameter_name"]
            ):
                lr_parameters.append(op)

        if len(lr_ops) > 1:
            lr_value = lr_ops[0].result(0)
            for op in lr_ops[1:]:
                lr = op.result(0)
                lr.replace_all_uses_with(lr_value)
                op.erase()

        if len(lr_parameters) > 1:
            lr_value = lr_parameters[0].result(0)
            for op in lr_parameters[1:]:
                lr = op.result(0)
                lr.replace_all_uses_with(lr_value)
                op.erase()

    @staticmethod
    def remove_no_need_in_startup(startup_program, main_program):
        # 1. vars used in main_program
        main_program_var_names = []
        for op in main_program.global_block().ops:
            for var in op.operands_source():
                if var.has_name:
                    main_program_var_names.append(var.name)
            for var in op.results():
                if var.has_name:
                    main_program_var_names.append(var.name)
        # 2. remove var op not used in main_program
        for op in startup_program.global_block().ops:
            for var in op.operands_source():
                if var.has_name and var.name not in main_program_var_names:
                    op.erase()
        # 3. dead code elimination
        pm = pir.PassManager()
        pm.add_pass('dead_code_elimination_pass', {})
        pm.run(startup_program)

    @staticmethod
    def remove_other_rank_input_output_pass(dist_program):
        '''
        Pruning value not belong to cur rank especially used for check_finite_and_unscale
        and update_loss_scaling op in amp.

        For example, w0 on mesh0, w1 on mesh1, before pass, the ops is:
            [w0_g, w1_g], is_finite = check_finite_and_scale([w0_g, w1_g], loss_scaling)
        after pass, on mesh0, the op is:
            [w0_g], is_finite = check_finite_and_scale([w0_g], loss_scaling)

        Note that here we do not set the op_dist_attr, since it is not used afterwards.
        '''
        cur_rank = paddle.distributed.get_rank()
        for op in dist_program.global_block().ops[::-1]:
            if op.name() not in amp_ops:
                continue
            new_vars = []
            combine_op = op.operand_source(0).get_defining_op()
            for inner_operand in (
                op.operand_source(0).get_defining_op().operands()
            ):
                if (
                    cur_rank
                    in inner_operand.source()
                    .dist_attr()
                    .process_mesh.process_ids
                ):
                    new_vars.append(inner_operand.source())
                    continue
            result = op.operand_source(0).get_defining_op().result(0)
            paddle.pir.set_insertion_point_after(combine_op)
            res = paddle._C_ops.builtin_combine(new_vars)
            res.get_defining_op().op_role = op.op_role
            result.replace_all_uses_with(res)
            combine_op.erase()
            # since it is inplace op, set type of output as the same as input
            op.result(0).set_type(res.type())

    @staticmethod
    def remove_other_rank_params_grads_pass(dist_program, dist_params_grads):
        cur_rank_param = []
        cur_rank = paddle.distributed.get_rank()

        for op in dist_program.global_block().ops:
            if op.name() == 'builtin.parameter':
                if cur_rank in op.dist_attr.process_mesh.process_ids:
                    cur_rank_param.append(op.attrs()['parameter_name'])

        need_remove_idx = []
        for idx, (param, grad) in enumerate(dist_params_grads):
            if grad is None:
                continue
            if param.name not in cur_rank_param:
                need_remove_idx.append(idx)

        for idx in need_remove_idx[::-1]:
            dist_params_grads.pop(idx)

    @staticmethod
    def apply_all(
        dist_main_program, dist_startup_program, dist_params_grads=[]
    ):
        RemovePasses.remove_other_rank_input_output_pass(dist_main_program)
        RemovePasses.remove_other_rank_params_grads_pass(
            dist_main_program, dist_params_grads
        )
        RemovePasses.remove_other_rank_op_pass(dist_main_program)
        RemovePasses.remove_no_need_in_startup(
            dist_startup_program, dist_main_program
        )


def replace_moe_global_mesh_tensor(op):
    cur_rank = paddle.distributed.get_rank()
    out_value = op.result(0)
    in_value = None
    in_idx = -1
    for idx, val in enumerate(op.operands_source()):
        val_mesh = val.dist_attr().process_mesh
        if cur_rank not in val_mesh.process_ids:
            continue
        assert (
            in_value is None
        ), f'{op} has more than one inputs on rank {cur_rank}'
        in_value = val
        in_idx = idx

    paddle.pir.set_insertion_point(op)
    local_value = paddle._C_ops.share_data_(in_value)
    # local_value = paddle.assign(in_value)
    local_value_type = paddle.base.libpaddle.pir.cvt_to_dist_type(
        out_value.type(), out_value.dist_attr()
    )
    local_value.set_type(local_value_type)
    out_value.replace_all_uses_with(local_value)

    op_dist_attr = op.dist_attr
    share_data_op = local_value.get_defining_op()
    share_data_op.dist_attr = (
        paddle.base.libpaddle.pir.create_op_dist_attribute(
            op_dist_attr.process_mesh,
            [op_dist_attr.operand(in_idx).as_tensor_dist_attr()],
            [op_dist_attr.result(0).as_tensor_dist_attr()],
        )
    )

    assert all(val.use_empty() for val in op.results())
    op.erase()


# Note: this is the pass in the dense program
comm_ops = [
    "pd_op.c_allreduce_sum",
    "pd_op.all_gather",
    "pd_op.c_allreduce_max",
    "pd_op.reduce_scatter",
]


def remove_unuseful_comm_op_pass(program):
    for op in program.global_block().ops:
        if op.name() in comm_ops:
            ring_id = op.int_attr("ring_id")
            process_group = get_process_group(ring_id)
            if process_group.nranks == 1:
                op.result(0).replace_all_uses_with(op.operand_source(0))
                op.erase()
        if op.name() == "pd_op.share_data_":
            if op.operand_source(0).has_one_use():
                op.result(0).replace_all_uses_with(op.operand_source(0))
                op.erase()


# In sequence_parallel, we need to transpose hidden_states
# from [bs, seq, hidden] to [seq, bs, hidden] to perform
# split and allgather at dim 0.
# The transpose may lead to about 3% performance
# in llama-70B model (tp4pp8).
# We found that, when bs=1, which is the common case in llm
# training, the transpose is equal to reshape.
# So, this pass is to haddle the specific case.
def eliminate_transpose_by_reshape(program):
    for op in program.global_block().ops:
        if (
            op.name() == 'pd_op.transpose'
            or op.name() == 'pd_op.transpose_grad'
        ):
            var = op.operand(0).source()
            rank = len(var.shape)
            perm = op.attrs()['perm']
            perm = [p + rank if p < 0 else p for p in perm]
            # only support transpose dim 0 and dim 1
            expected_perm = [1, 0] + [i + 2 for i in range(rank - 2)]
            if perm == expected_perm and (
                var.shape[0] == 1 or var.shape[1] == 1
            ):
                paddle.pir.set_insertion_point(op)
                transpose_var = op.result(0)
                reshape_var = paddle._C_ops.reshape(var, transpose_var.shape)
                reshape_var.get_defining_op().op_role = op.op_role
                transpose_var.replace_all_uses_with(reshape_var)
                op.erase()
    return program


def complete_op_role(main_program, op_role_scope: list):
    assert (
        len(op_role_scope) == 3 and len(op_role_scope[0]) == 2
    ), "op_role_scope should has the shape[3, 2]"
    forward_op_start = op_role_scope[0][0]
    forward_op_end = op_role_scope[0][1]

    backward_op_start = op_role_scope[1][0]
    backward_op_end = op_role_scope[1][1]

    opt_op_start = op_role_scope[2][0]
    opt_op_end = op_role_scope[2][1]

    global_op_idx = 0
    for blk in main_program.blocks:
        for op in blk.ops:
            if (
                global_op_idx >= forward_op_start
                and global_op_idx < forward_op_end
            ):
                op.op_role = 0
            elif (
                global_op_idx >= backward_op_start
                and global_op_idx < backward_op_end
            ):
                op.op_role = 1
            elif global_op_idx >= opt_op_start and global_op_idx < opt_op_end:
                op.op_role = 2
            else:
                pass
            global_op_idx += 1


def pipeline_pass(dense_main_program, dense_starup_program, pipeline_strategy):
    """
    Pipeline schedule pass for auto parallel. Enables the pipeline parallel scheduling
    strategies like FThenB, 1F1B, VPP, etc.
    """
    import os

    pass_name = pipeline_strategy.schedule_mode
    assert pass_name in [
        "FThenB",
        "1F1B",
        "VPP",
    ], f"pipeline scheduler only support FThenB, 1F1B and VPP now, but receive {pass_name}"

    pass_attr = {}
    pass_attr["num_micro_batches"] = pipeline_strategy.accumulate_steps
    pass_attr["pp_degree"] = pipeline_strategy.pp_degree
    pass_attr["pp_stage"] = get_pp_stage_by_pp_degree(
        pipeline_strategy.pp_degree
    )
    pass_attr["vpp_degree"] = pipeline_strategy.vpp_degree

    if pass_name == "1F1B":
        # TODO(Ruibiao): Move FLAGS_1f1b_backward_forward_overlap and
        # FLAGS_mp_async_allreduce_in_backward to auto parallel Strategy
        # after these two optimizations are available.
        pass_attr["enable_backward_forward_overlap"] = int(
            os.environ.get("FLAGS_1f1b_backward_forward_overlap", 0)
        )

    pipeline_pass = new_pass("pipeline_scheduler_" + pass_name, pass_attr)
    pass_context = PassContext()
    pipeline_pass.apply(
        dense_main_program,
        dense_starup_program,
        pass_context,
    )
    plan = pass_context.get_attr("plan")
    return plan


def _extract_seg_method(op, seg_method):
    regex = re.compile(seg_method, re.IGNORECASE)
    struct_name = (
        op.attrs()["struct_name"] if op.has_attr("struct_name") else "/"
    )
    m = regex.search(struct_name)
    if not m:
        return None
    return struct_name[m.start(0) :].split("/")[0]


def _get_seg_struct_names(ops, seg_method):
    fwd_start_op_index = 0
    for i, op in enumerate(ops):
        if _extract_seg_method(op, seg_method):
            fwd_start_op_index = i
            break

    total_op_num = len(ops)
    fwd_end_op_index = total_op_num - 1
    for i in reversed(range(total_op_num)):
        if _extract_seg_method(ops[i], seg_method):
            fwd_end_op_index = i
            break

    struct_names = collections.OrderedDict()
    seg_op_mesh = collections.OrderedDict()

    for i in range(fwd_start_op_index, fwd_end_op_index + 1):
        if ops[i].name() == "builtin.combine":
            continue

        struct_name = _extract_seg_method(ops[i], seg_method)
        if struct_name:
            struct_names[struct_name] = 1
            if struct_name in seg_op_mesh:
                assert (
                    seg_op_mesh[struct_name] == ops[i].dist_attr.process_mesh
                ), "The segment's ops should have same process_mesh."

            seg_op_mesh[struct_name] = ops[i].dist_attr.process_mesh
        else:
            if ops[i].name() != "dist_op.reshard":
                raise ValueError(
                    f"The op {ops[i].name()} without seg_method in its struct_name should only be reshard"
                )

    return list(struct_names.keys())


def _analyze_use_custom_mesh(ops, seg_method, pp_degree):
    non_use_custom_mesh = True
    seg_pp_stages = [-1]

    for op in ops:
        if _extract_seg_method(op, seg_method) and "pd_op" in op.name():
            op_mesh = op.dist_attr.process_mesh
            pp_stage = get_pp_stage_by_process_mesh(op_mesh, pp_degree)
            if pp_stage is None:
                continue

            if seg_pp_stages[-1] > pp_stage:
                non_use_custom_mesh = False
                break
            seg_pp_stages.append(pp_stage)

    if not non_use_custom_mesh:
        _logger.info("Cannot Use Auto VPP")
    else:
        _logger.info("Using Auto VPP")

    return non_use_custom_mesh


def _set_process_mesh_and_chunk_id(op, process_mesh, chunk_id, set_mesh):
    def set_var_origin_op_process_mesh(var_origin_op):
        var_origin_op_input_attr = var_origin_op.dist_attr.operands()
        var_origin_op_output_attr = var_origin_op.dist_attr.results()
        var_origin_op_output_attr[0] = var_origin_op_output_attr[
            0
        ].as_tensor_dist_attr()
        var_origin_op_output_attr[0] = (
            paddle.base.libpaddle.pir.create_tensor_dist_attribute(
                process_mesh,
                var_origin_op_output_attr[0].dims_mapping,
                var_origin_op_output_attr[0].partial_status,
            )
        )

        var_origin_op.dist_attr = (
            paddle.base.libpaddle.pir.create_op_dist_attribute(
                process_mesh,
                var_origin_op_input_attr,
                var_origin_op_output_attr,
                0,
            )
        )

    def set_process_mesh(vars, attrs):
        for idx, (var, attr) in enumerate(zip(vars, attrs)):
            var_dist_attr = var.dist_attr()
            # Note(luchang): the var generated by builtin.combine will have mutilple dist_attr
            if var_dist_attr and var_dist_attr.as_array_attr():
                var_array_attr = var_dist_attr.as_array_attr()
                for i in range(len(var_array_attr)):
                    var_dist_attr = var_array_attr[i].as_tensor_dist_attr()
                    if var_dist_attr.process_mesh == op_mesh:
                        var_array_attr[i] = copy_dist_attr_with_new_member(
                            var_dist_attr, new_process_mesh=process_mesh
                        )
                var.update_dist_attr(var_array_attr)
            elif var_dist_attr and var_dist_attr.process_mesh == op_mesh:
                var.update_dist_attr(
                    copy_dist_attr_with_new_member(
                        var_dist_attr, new_process_mesh=process_mesh
                    )
                )
                var_origin_op = var.get_defining_op()
                if var_origin_op.name() in ["pd_op.data", "builtin.parameter"]:
                    set_var_origin_op_process_mesh(var_origin_op)

            if attr.as_array_attr():
                array_attr = attr.as_array_attr()
                new_array_attr = []
                for i in range(len(array_attr)):
                    tensor_attr = array_attr[i].as_tensor_dist_attr()
                    new_array_attr.append(tensor_attr)
                    if tensor_attr and tensor_attr.process_mesh == op_mesh:
                        new_array_attr[i] = copy_dist_attr_with_new_member(
                            tensor_attr, new_process_mesh=process_mesh
                        )
                attrs[idx] = (
                    paddle.base.libpaddle.pir.create_array_dist_attribute(
                        new_array_attr
                    )
                )
            else:
                tensor_attr = attr.as_tensor_dist_attr()
                if tensor_attr and tensor_attr.process_mesh == op_mesh:
                    attrs[idx] = copy_dist_attr_with_new_member(
                        tensor_attr, new_process_mesh=process_mesh
                    )

    op_dist_attr = op.dist_attr
    op_mesh = op_dist_attr.process_mesh
    op_input_attrs = op_dist_attr.operands()
    op_output_attrs = op_dist_attr.results()
    op_input_vars = op.operands_source()
    op_output_vars = op.results()

    if set_mesh:
        set_process_mesh(op_input_vars, op_input_attrs)
        set_process_mesh(op_output_vars, op_output_attrs)
        op_mesh = process_mesh

    op.dist_attr = paddle.base.libpaddle.pir.create_op_dist_attribute(
        op_mesh,
        op_input_attrs,
        op_output_attrs,
        chunk_id,
    )


def complete_chunk_id(dist_program, pipeline_strategy):
    if not pipeline_strategy.enable:
        return

    sub_process_meshes = get_sub_process_mesh_by_program(dist_program)
    pp_degree = pipeline_strategy.pp_degree
    vpp_degree = pipeline_strategy.vpp_degree
    seg_method = pipeline_strategy.vpp_seg_method
    schedule_mode = pipeline_strategy.schedule_mode
    num_chunks = pp_degree * vpp_degree

    if pp_degree < 2 and vpp_degree > 1:
        raise ValueError("VPP schedule mode only can be set in pipeline mode.")
    if vpp_degree > 1 and (not seg_method or schedule_mode != "VPP"):
        raise ValueError(
            "Please set right schedule_mode and vpp_seg_method for VPP."
        )
    if vpp_degree < 2:
        return

    ReshardPasses.fold_reshard_pass(dist_program)
    seg_struct_names = _get_seg_struct_names(
        dist_program.global_block().ops, seg_method
    )
    ops = dist_program.global_block().ops

    assert (
        len(seg_struct_names) % num_chunks == 0
    ), f"The number of layers[{seg_method}] ({len(seg_struct_names)}) should be divided by part number ({num_chunks})."

    # Step2: analysis whether the pp_stage is non-decreasing among segments
    # 1. if non_use_custom_mesh is True, the ops' process_mesh will be changed by vpp strategy
    # 2. if non_use_custom_mesh is False, the ops's process_mesh will not be changed.
    non_use_custom_mesh = _analyze_use_custom_mesh(ops, seg_method, pp_degree)

    # Step3: Get op index boundary, pp_stage, chunk_id, struct_names of each segment
    seg_pp_stages = [i % pp_degree for i in range(num_chunks)]
    seg_chunk_ids = [i // pp_degree for i in range(num_chunks)]
    seg_layer_num = len(seg_struct_names) // num_chunks
    seg_parts = [0]

    for idx, op in enumerate(ops):
        if len(seg_parts) == len(seg_struct_names):
            break
        struct_name = _extract_seg_method(op, seg_method)
        if struct_name == seg_struct_names[len(seg_parts)]:
            seg_parts.append(idx)
    seg_parts.append(len(ops))

    # Step4: Set the process_mesh of each op
    seg_id = 0
    reshard_ops = []
    for seg_id in range(num_chunks):
        start_idx = seg_parts[seg_id * seg_layer_num]
        end_idx = seg_parts[seg_id * seg_layer_num + seg_layer_num]
        pp_stage = seg_pp_stages[seg_id]
        chunk_id = seg_chunk_ids[seg_id]
        struct_name = ",".join(
            seg_struct_names[
                seg_id * seg_layer_num : seg_id * seg_layer_num + seg_layer_num
            ]
        )
        process_mesh = sub_process_meshes[pp_stage]

        _logger.info(
            f"stage=[{pp_stage}], chunk_id=[{chunk_id}], layer_name=[{struct_name}]"
        )
        _logger.info(
            f"start op: [{ops[start_idx].name()}], end op: [{ops[end_idx - 1].name()}]"
        )

        for idx in range(start_idx, end_idx):
            if ops[idx].name() in dist_skip_op_list:
                continue
            if ops[idx].name() == "dist_op.reshard":
                reshard_ops.append(ops[idx])
                continue

            is_seg_op = _extract_seg_method(ops[idx], seg_method) is not None
            for sub_block in ops[idx].blocks():
                # TODO(luchang): support condition block
                pass

            _set_process_mesh_and_chunk_id(
                ops[idx],
                process_mesh,
                chunk_id,
                non_use_custom_mesh & is_seg_op,
            )

    # Step5: set right process_mesh for reshard op
    for op in reshard_ops:
        var = op.operand_source(0)

        op_dist_attr = op.dist_attr
        src_dist_attr = op_dist_attr.operand(0).as_tensor_dist_attr()
        dst_dist_attr = op_dist_attr.result(0).as_tensor_dist_attr()

        if src_dist_attr == dst_dist_attr:
            op.result(0).replace_all_uses_with(var)
            op.erase()
            continue

        reshard_func = choose_reshard_func(src_dist_attr, dst_dist_attr)
        reshard_func_name = reshard_func.__class__.__name__

        if reshard_func_name == "NdMeshReshardFunction":
            new_process_mesh = var.dist_attr().process_mesh
            new_src_dist_attr = copy_dist_attr_with_new_member(
                src_dist_attr, new_process_mesh=new_process_mesh
            )
            new_dst_dist_attr = copy_dist_attr_with_new_member(
                dst_dist_attr, new_process_mesh=new_process_mesh
            )
            op.dist_attr = copy_op_attr_with_new_member(
                op_dist_attr,
                new_operands=[new_src_dist_attr],
                new_results=[new_dst_dist_attr],
                new_process_mesh=new_process_mesh,
            )
        elif reshard_func_name == "GlobaleToSubMeshFunction":
            result_var = op.result(0)
            new_process_mesh = result_var.dist_attr().process_mesh
            new_dst_dist_attr = copy_dist_attr_with_new_member(
                dst_dist_attr, new_process_mesh=new_process_mesh
            )
            op.dist_attr = copy_op_attr_with_new_member(
                op_dist_attr, new_results=[new_dst_dist_attr]
            )
        elif reshard_func_name == "NdMeshReshardFunctionCrossMesh":
            result_var = op.result(0)
            src_process_mesh = var.dist_attr().process_mesh
            dst_process_mesh = result_var.dist_attr().process_mesh
            new_src_dist_attr = copy_dist_attr_with_new_member(
                src_dist_attr, new_process_mesh=src_process_mesh
            )
            new_dst_dist_attr = copy_dist_attr_with_new_member(
                dst_dist_attr, new_process_mesh=dst_process_mesh
            )
            op.dist_attr = copy_op_attr_with_new_member(
                op_dist_attr,
                new_operands=[new_src_dist_attr],
                new_results=[new_dst_dist_attr],
            )
        elif reshard_func_name == "SameStatusReshardFunction":
            op.result(0).replace_all_uses_with(var)
            op.erase()
        else:
            raise ValueError(
                f"Unsupport reshard function: {reshard_func_name}, reshard op's dist_attr: {op.dist_attr}"
            )

    # Step6: add reshard op between pipeline chunks
    apply_partition_pass(dist_program)


def check_chunk_id(dist_program):
    all_ops = dist_program.global_block().ops

    for idx, op in enumerate(all_ops):
        if op.op_role in [int(OpRole.Forward), int(OpRole.Backward)]:
            if op.name() in dist_skip_op_list:
                continue

            if op.dist_attr.chunk_id == -1:
                if op.name() in ["pd_op.data", "builtin.parameter"]:
                    op.dist_attr = copy_op_attr_with_new_member(
                        op.dist_attr, new_chunk_id=0
                    )
                elif op.name() in ["pd_op.full", "pd_op.full_int_array"]:
                    all_used_ops = op.result(0).all_used_ops()
                    for used_op in all_used_ops:
                        if used_op.dist_attr.chunk_id != -1:
                            op.dist_attr = paddle.base.libpaddle.pir.create_op_dist_attribute(
                                op.dist_attr.process_mesh,
                                op.dist_attr.operands(),
                                op.dist_attr.results(),
                                used_op.dist_attr.chunk_id,
                            )
                            break
                    if op.dist_attr.chunk_id == -1:
                        raise ValueError(
                            f"The chunk_id of op[{op.name()}] is not set. Please check the chunk_id setting."
                        )
                else:
                    raise ValueError(
                        f"The chunk_id of op[{op.name()}] is not set. Please check the chunk_id setting."
                    )
