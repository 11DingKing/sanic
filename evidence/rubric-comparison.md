项目：sanic-org-sanic-2c406f3d-31ee-45ff-893a-78c50591f310｜Sanic 信号监听器异常后的派发收尾
复现验证：/tmp/swe-delivery-review-sanic04-py311/bin/pytest -q -p no:cacheprovider tests/test_signals.py tests/test_signal_handlers.py tests/test_multi_serve.py tests/test_multiprocessing.py
原始结果：72 passed, 11 warnings in 6.15s（独立 Python 3.11 环境，加载本项目 editable 源码）。

Rubric ID：1｜状态：通过｜期望行为：单个监听器失败时其他已注册监听器各得到一次可观察的派发结果｜实际观察：第一个监听器抛出 ValueError 后，first、second、third 依次调用，派发返回失败结果｜直接证据：sanic/signals.py:264-324 保存异常并继续遍历；tests/test_signals.py:797-820 断言三次调用与错误结果。
Rubric ID：2｜状态：通过｜期望行为：失败沿当前派发返回或记录且不被后续监听器静默覆盖｜实际观察：首个 ValueError 返回，两个失败均进入异常报告，第二个异常保存在 __suppressed_exceptions__｜直接证据：sanic/signals.py:295-324；tests/test_signals.py:824-872。
Rubric ID：3｜状态：通过｜期望行为：取消当前派发后未开始的监听器不伪造成功｜实际观察：首个等待中的监听器收到取消后派发抛出 CancelledError，第二监听器未运行且未发出失败报告｜直接证据：sanic/signals.py:282-290 对取消立即重抛；tests/test_signals.py:876-908 使用 Event 控制取消时点并断言调用结果。
Rubric ID：4｜状态：通过｜期望行为：同一信号再次派发不重复触发已经完成的 waiter｜实际观察：两次连续派发使 handler 调用两次，完成 waiter 只收到一次结果；取消的 waiter 不阻断派发｜直接证据：sanic/signals.py:115-130,259-262 仅解析未完成 Future；tests/test_signals.py:911-962。
Rubric ID：5｜状态：通过｜期望行为：应用关闭派发按既有顺序完成所有清理监听器并保留失败结果｜实际观察：反向关闭派发中首个清理监听器抛错后其余清理监听器仍执行并返回原错误；信号处理、多服务关闭和多进程测试通过｜直接证据：sanic/signals.py:249-251,264-324 保持顺序并完成等待；tests/test_signals.py:980-1000；本次 tests/test_signal_handlers.py、tests/test_multi_serve.py、tests/test_multiprocessing.py 均通过。
Rubric ID：6｜状态：通过｜期望行为：正常信号顺序和监听器参数保持原语义｜实际观察：正序与逆序派发均按注册优先级调用，参数传入既有派发路径｜直接证据：sanic/signals.py:245-251,264-280；tests/test_signals.py:776-793。
Rubric ID：7｜状态：通过｜期望行为：未注册该信号的应用保持原有启动和关闭行为｜实际观察：既有信号注册、服务启动关闭及多进程用例随本次 72 项验证通过｜直接证据：tests/test_signal_handlers.py、tests/test_multi_serve.py、tests/test_multiprocessing.py；完整命令与原始结果见本报告开头。
