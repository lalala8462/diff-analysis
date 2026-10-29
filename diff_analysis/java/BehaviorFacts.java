import com.sun.source.tree.*;
import com.sun.source.util.*;
import javax.lang.model.element.*;
import javax.lang.model.type.TypeKind;
import javax.lang.model.util.Elements;
import java.util.*;

/** 从固定源码提取结构事实，不执行项目代码或注解处理器。 */
final class BehaviorFacts {
    private final CompilationUnitTree unit;
    private final Trees trees;
    private final Elements elements;
    private int nodes;

    BehaviorFacts(CompilationUnitTree unit, Trees trees, Elements elements) {
        this.unit = unit; this.trees = trees; this.elements = elements;
    }

    private Map<String, Object> map(Object... pairs) {
        Map<String, Object> result = new LinkedHashMap<String, Object>();
        for (int i = 0; i < pairs.length; i += 2) result.put((String)pairs[i], pairs[i + 1]);
        return result;
    }

    private String binding(Tree tree) {
        try {
            Element e = trees.getElement(TreePath.getPath(unit, tree));
            if (e instanceof ExecutableElement) {
                ExecutableElement m = (ExecutableElement)e;
                if (m.getEnclosingElement().asType().getKind() == TypeKind.ERROR) return "";
                List<String> types = new ArrayList<String>();
                for (VariableElement p : m.getParameters()) {
                    if (p.asType().getKind() == TypeKind.ERROR) return "";
                    types.add(p.asType().toString());
                }
                return elements.getBinaryName((TypeElement)m.getEnclosingElement()).toString() + "#" + m.getSimpleName()
                    + "(" + String.join(",", types) + ")";
            }
            if (e instanceof VariableElement && e.getKind() == ElementKind.ENUM_CONSTANT)
                return e.getEnclosingElement().toString() + "." + e.getSimpleName();
        } catch (RuntimeException ignored) { }
        return "";
    }

    Map<String, Object> method(MethodTree tree) {
        List<Object> parameters = new ArrayList<Object>();
        for (VariableTree p : tree.getParameters())
            parameters.add(map("name", p.getName().toString(), "type", p.getType().toString()));
        List<Object> instanceFields = new ArrayList<Object>();
        TreePath parent = TreePath.getPath(unit, tree).getParentPath();
        if (parent != null && parent.getLeaf() instanceof ClassTree)
            for (Tree member : ((ClassTree)parent.getLeaf()).getMembers())
                if (member instanceof VariableTree && !((VariableTree)member).getModifiers().getFlags().contains(Modifier.STATIC))
                    instanceFields.add(node(member, 0));
        return map("version", "behavior-ast-v2", "parameters", parameters, "fields", instanceFields,
            "static", tree.getModifiers().getFlags().contains(Modifier.STATIC),
            "return_type", tree.getReturnType() == null ? "void" : tree.getReturnType().toString(),
            "body", node(tree.getBody(), 0));
    }

    private List<Object> list(Iterable<? extends Tree> trees, int depth) {
        List<Object> result = new ArrayList<Object>();
        for (Tree tree : trees) result.add(node(tree, depth + 1));
        return result;
    }

    private Map<String, Object> node(Tree t, int depth) {
        if (t == null) return null;
        long pos = trees.getSourcePositions().getStartPosition(unit, t);
        Map<String, Object> n = map("kind", t.getKind().name(), "line",
            pos < 0 ? 0 : (int)unit.getLineMap().getLineNumber(pos), "offset", pos);
        if (++nodes > 2000 || depth > 64) { n.put("kind", "UNSUPPORTED"); n.put("reason", "AST_BUDGET"); return n; }
        if (t instanceof ExpressionTree) {
            try {
                javax.lang.model.type.TypeMirror type = trees.getTypeMirror(TreePath.getPath(unit, t));
                if (type != null && type.getKind() != TypeKind.ERROR) n.put("java_type", type.toString());
            } catch (RuntimeException ignored) { }
        }
        if (t instanceof BlockTree) n.put("items", list(((BlockTree)t).getStatements(), depth));
        else if (t instanceof VariableTree) {
            VariableTree v = (VariableTree)t;
            n.put("name", v.getName().toString()); n.put("type", v.getType().toString());
            n.put("value", node(v.getInitializer(), depth + 1));
        } else if (t instanceof ExpressionStatementTree) n.put("value", node(((ExpressionStatementTree)t).getExpression(), depth + 1));
        else if (t instanceof IfTree) {
            IfTree v = (IfTree)t;
            n.put("condition", node(v.getCondition(), depth + 1));
            n.put("then", node(v.getThenStatement(), depth + 1)); n.put("else", node(v.getElseStatement(), depth + 1));
        } else if (t instanceof ReturnTree) n.put("value", node(((ReturnTree)t).getExpression(), depth + 1));
        else if (t instanceof ThrowTree) n.put("value", node(((ThrowTree)t).getExpression(), depth + 1));
        else if (t instanceof ParenthesizedTree) return node(((ParenthesizedTree)t).getExpression(), depth + 1);
        else if (t instanceof LiteralTree) n.put("value", ((LiteralTree)t).getValue());
        else if (t instanceof IdentifierTree) {
            n.put("name", t.toString()); n.put("binding", binding(t));
            try {
                Element e = trees.getElement(TreePath.getPath(unit, t));
                n.put("field", e != null && e.getKind() == ElementKind.FIELD);
                if (e instanceof VariableElement && ((VariableElement)e).getConstantValue() != null) {
                    n.put("constant", ((VariableElement)e).getConstantValue()); n.put("constant_known", true);
                }
            } catch (RuntimeException ignored) { }
        }
        else if (t instanceof MemberSelectTree) {
            MemberSelectTree v = (MemberSelectTree)t;
            n.put("receiver", node(v.getExpression(), depth + 1)); n.put("name", v.getIdentifier().toString());
            n.put("binding", binding(t));
            try {
                Element e = trees.getElement(TreePath.getPath(unit, t));
                if (e instanceof VariableElement && ((VariableElement)e).getConstantValue() != null) {
                    n.put("constant", ((VariableElement)e).getConstantValue()); n.put("constant_known", true);
                }
            } catch (RuntimeException ignored) { }
        } else if (t instanceof MethodInvocationTree) {
            MethodInvocationTree v = (MethodInvocationTree)t;
            n.put("select", node(v.getMethodSelect(), depth + 1)); n.put("arguments", list(v.getArguments(), depth));
            n.put("binding", binding(t));
            try {
                Element e = trees.getElement(TreePath.getPath(unit, t));
                n.put("static", e != null && e.getModifiers().contains(Modifier.STATIC));
                n.put("return_type", trees.getTypeMirror(TreePath.getPath(unit, t)).toString());
            } catch (RuntimeException ignored) { n.put("static", false); }
        } else if (t instanceof ArrayAccessTree) {
            ArrayAccessTree v = (ArrayAccessTree)t;
            n.put("array", node(v.getExpression(), depth + 1)); n.put("index", node(v.getIndex(), depth + 1));
        } else if (t instanceof BinaryTree) {
            BinaryTree v = (BinaryTree)t;
            n.put("left", node(v.getLeftOperand(), depth + 1)); n.put("right", node(v.getRightOperand(), depth + 1));
            try { n.put("type", trees.getTypeMirror(TreePath.getPath(unit, t)).toString()); }
            catch (RuntimeException ignored) { n.put("type", ""); }
        } else if (t instanceof UnaryTree) n.put("value", node(((UnaryTree)t).getExpression(), depth + 1));
        else if (t instanceof AssignmentTree) {
            AssignmentTree v = (AssignmentTree)t;
            n.put("target", node(v.getVariable(), depth + 1)); n.put("value", node(v.getExpression(), depth + 1));
        } else if (t instanceof ConditionalExpressionTree) {
            ConditionalExpressionTree v = (ConditionalExpressionTree)t;
            n.put("condition", node(v.getCondition(), depth + 1));
            n.put("then", node(v.getTrueExpression(), depth + 1)); n.put("else", node(v.getFalseExpression(), depth + 1));
        } else if (t instanceof NewClassTree) {
            NewClassTree v = (NewClassTree)t;
            n.put("type", v.getIdentifier().toString()); n.put("arguments", list(v.getArguments(), depth));
            n.put("binding", binding(t));
        } else if (t instanceof NewArrayTree) {
            NewArrayTree v = (NewArrayTree)t;
            n.put("dimensions", list(v.getDimensions(), depth));
            n.put("items", v.getInitializers() == null ? null : list(v.getInitializers(), depth));
        } else if (t instanceof TypeCastTree) {
            TypeCastTree v = (TypeCastTree)t;
            n.put("type", v.getType().toString()); n.put("value", node(v.getExpression(), depth + 1));
        } else if (t instanceof SwitchTree) {
            SwitchTree v = (SwitchTree)t;
            n.put("value", node(v.getExpression(), depth + 1)); n.put("cases", list(v.getCases(), depth));
        } else if (t instanceof CaseTree) {
            CaseTree v = (CaseTree)t;
            n.put("value", node(v.getExpression(), depth + 1)); n.put("items", list(v.getStatements(), depth));
        } else if (t instanceof TryTree) {
            TryTree v = (TryTree)t;
            n.put("resources", list(v.getResources(), depth)); n.put("body", node(v.getBlock(), depth + 1));
            n.put("catches", list(v.getCatches(), depth)); n.put("finally", node(v.getFinallyBlock(), depth + 1));
        } else if (t instanceof CatchTree) {
            CatchTree v = (CatchTree)t;
            n.put("type", v.getParameter().getType().toString()); n.put("name", v.getParameter().getName().toString());
            try { n.put("type", trees.getTypeMirror(TreePath.getPath(unit, v.getParameter().getType())).toString()); }
            catch (RuntimeException ignored) { }
            n.put("body", node(v.getBlock(), depth + 1));
        } else if (t.getKind() != Tree.Kind.BREAK && t.getKind() != Tree.Kind.EMPTY_STATEMENT) {
            n.put("kind", "UNSUPPORTED"); n.put("reason", t.getKind().name());
        }
        return n;
    }
}
