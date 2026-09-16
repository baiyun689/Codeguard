import java.util.Arrays;
import org.apache.commons.codec.binary.Base64;
import org.apache.commons.codec.binary.Hex;
import org.apache.commons.codec.binary.StringUtils;

/** 验证跨文件调用和公共接口使用行为，依赖实际项目编译结果。 */
public class CodecBehaviorOracle {
    interface Check { boolean run() throws Exception; }
    static void check(String name, Check check) {
        try { System.out.println(name + "=" + (check.run() ? "PASS" : "FAIL")); }
        catch (Throwable e) { System.out.println(name + "=FAIL:" + e.getClass().getSimpleName()); }
    }
    public static void main(String[] args) {
        check("utf8-null", () -> StringUtils.getBytesUtf8(null) == null);
        check("base64-null-consumer", () -> Base64.decodeBase64((String)null) == null);
        check("utf8-value", () -> Arrays.equals(StringUtils.getBytesUtf8("a"),new byte[]{97}));
        check("hex-range", () -> new String(Hex.encodeHex(new byte[]{1,0x23,0x45,0x67},1,2,true)).equals("2345"));
        check("hex-full", () -> Hex.encodeHexString(new byte[]{0x23,0x45}).equals("2345"));
        check("hex-roundtrip", () -> Arrays.equals(Hex.decodeHex("abcd"),new byte[]{(byte)0xab,(byte)0xcd}));
    }
}
