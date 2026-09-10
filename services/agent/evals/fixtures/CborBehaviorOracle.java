import java.io.*;
import java.util.*;
import co.nstant.in.cbor.*;
import co.nstant.in.cbor.model.*;

/** Executed against the actual compiled project, outside the reviewer workspace. */
public class CborBehaviorOracle {
    interface Check { boolean run() throws Exception; }
    static void check(String name, Check test) {
        try { System.out.println(name + "=" + (test.run() ? "PASS" : "FAIL")); }
        catch (Throwable failure) { System.out.println(name + "=FAIL:" + failure.getClass().getSimpleName()); }
    }
    public static void main(String[] args) {
        check("array-setting", () -> {
            CborDecoder d = new CborDecoder(new ByteArrayInputStream(new byte[]{(byte)0x9f, 1, (byte)0xff}));
            d.setAutoDecodeInfinitiveArrays(false);
            return !d.isAutoDecodeInfinitiveArrays() && ((Array)d.decodeNext()).getDataItems().isEmpty();
        });
        check("null-encoding", () -> {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            new CborEncoder(out).encode((DataItem)null);
            return Arrays.equals(out.toByteArray(), new byte[]{(byte)0xf6});
        });
        check("truncated-integer", () -> {
            try { CborDecoder.decode(new byte[]{(byte)0x18}); return false; }
            catch (CborException expected) { return true; }
        });
        check("map-overwrite", () -> {
            co.nstant.in.cbor.model.Map m = new co.nstant.in.cbor.model.Map();
            DataItem k = new UnsignedInteger(1), a = new UnsignedInteger(2), b = new UnsignedInteger(3);
            m.put(k,a).put(k,b);
            return b.equals(m.get(k)) && m.getKeys().size()==1;
        });
        check("map-remove-consumer", () -> {
            co.nstant.in.cbor.model.Map m = new co.nstant.in.cbor.model.Map();
            DataItem k = new UnsignedInteger(1), v = new UnsignedInteger(2);
            m.put(k,v); m.remove(k);
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            new CborEncoder(out).nonCanonical().encode(m);
            return Arrays.equals(out.toByteArray(), new byte[]{(byte)0xa0});
        });
        check("list-stream", () -> {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            new CborEncoder(out).encode(Arrays.asList(new UnsignedInteger(1),new UnsignedInteger(2),new UnsignedInteger(3)));
            return Arrays.equals(out.toByteArray(),new byte[]{1,2,3});
        });
        check("null-list-order", () -> {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            new CborEncoder(out).encode(Arrays.asList(new UnsignedInteger(1),null,new UnsignedInteger(2)));
            return Arrays.equals(out.toByteArray(),new byte[]{1,(byte)0xf6,2});
        });
    }
}
