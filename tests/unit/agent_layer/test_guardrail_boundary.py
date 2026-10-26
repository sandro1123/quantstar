"""AG-GUARD 20260907 — PII 正则数字边界断言防回归
病案: 全精度浮点因子串 '13988191986' (小数位) 误中手机号正则 → 决策 LLM 75/75 全拦
"""
from agent_layer.security.guardrail import Guardrail


def test_factor_float_precision_not_flagged():
    """病案复现: 生产 09-04 603045 同款全精度因子串必须放行"""
    g = Guardrail()
    s = str({"cntp20": -0.09671398819198654,
             "cntd20": -0.08104065530979553,
             "alpha_004": 0.04020403599001135})
    assert g.check(s).passed is True


def test_prompt_shape_with_scores_not_flagged():
    """完整决策 prompt 形态 (模板拼接后) 必须放行"""
    g = Guardrail()
    s = ("股票: 603045 (603045)\n因子评分: {'cntp20': -0.096714, 'volatility': -0.271611}"
         "\n市场状态: oscillating\n股票: 002716 composite=0.2716118230118262")
    assert g.check(s).passed is True


def test_real_phone_still_flagged():
    """真手机号 (前后文字/标点包裹) 必须照拦"""
    g = Guardrail()
    assert g.check("联系电话 13812345678 谢谢").passed is False


def test_real_id_still_flagged():
    """真身份证 (前后文字包裹) 必须照拦"""
    g = Guardrail()
    assert g.check("身份证号11010119900101123X").passed is False
    assert g.check("尾号 X 的证号 11010119900101123X。").passed is False


def test_id_with_trailing_letter_boundary():
    """身份证尾部 X 后接中文 (非数字/点) 仍拦; 但数字环境内不误拦"""
    g = Guardrail()
    # 数字长串中段 (浮点环境) 放行
    assert g.check("均值 -0.9601234567890123445 变动").passed is True
