package unluac.parse;

import unluac.decompile.PrintFlag;

public class LVector extends LObject {
  
  public final String name;
  public final float[] values;
  public final int nargs;
  
  public LVector(String name, float[] values) {
    this.name = name;
    this.values = values;
    this.nargs = values.length;
  }
  
  @Override
  public String toPrintString(int flags) {
    StringBuilder sb = new StringBuilder();
    sb.append(name).append("(");
    for(int i = 0; i < values.length; i++) {
      if(i > 0) sb.append(", ");
      // Format float, remove trailing zeros
      String s = Float.toString(values[i]);
      if(s.endsWith(".0")) s = s.substring(0, s.length() - 2);
      sb.append(s);
    }
    sb.append(")");
    return sb.toString();
  }
  
  @Override
  public boolean equals(Object o) {
    if(o instanceof LVector) {
      LVector other = (LVector) o;
      if(nargs != other.nargs || !name.equals(other.name)) return false;
      for(int i = 0; i < nargs; i++) {
        if(values[i] != other.values[i]) return false;
      }
      return true;
    }
    return false;
  }
  
  @Override
  public int hashCode() {
    int h = name.hashCode();
    for(float v : values) h = h * 31 + Float.floatToIntBits(v);
    return h;
  }
}
