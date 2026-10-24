import com.sun.source.tree.*;
import com.sun.source.util.*;
import javax.tools.*;
import javax.lang.model.element.*;
import javax.lang.model.type.*;
import javax.lang.model.util.Elements;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;

/** Java 8 compatible AST scanner. Parse only; never compile business code. */
public final class JavaAstScanner {
    private static String json(String value) {
        if (value == null) return "null";
        StringBuilder b = new StringBuilder("\"");
        for (char c : value.toCharArray()) {
            if (c == '"' || c == '\\') b.append('\\').append(c);
            else if (c == '\n') b.append("\\n");
            else if (c == '\r') b.append("\\r");
            else if (c == '\t') b.append("\\t");
            else if (c < 32) b.append(String.format("\\u%04x", (int)c));
            else b.append(c);
        }
        return b.append('"').toString();
    }

    static void emit(String kind, Map<String, String> fields, Map<String, Integer> nums) {
        StringBuilder b = new StringBuilder("{\"kind\":").append(json(kind));
        for (Map.Entry<String, String> e : fields.entrySet())
            b.append(',').append(json(e.getKey())).append(':').append(json(e.getValue()));
        for (Map.Entry<String, Integer> e : nums.entrySet())
            b.append(',').append(json(e.getKey())).append(':').append(e.getValue());
        System.out.println(b.append('}').toString());
    }

    static Map<String, String> fields(String... values) {
        Map<String, String> m = new LinkedHashMap<String, String>();
        for (int i = 0; i < values.length; i += 2) m.put(values[i], values[i + 1]);
        return m;
    }

    static Map<String, Integer> ints(String k, int v, String k2, int v2) {
        Map<String, Integer> m = new LinkedHashMap<String, Integer>();
        m.put(k, v); m.put(k2, v2);
        return m;
    }

    /** Canonical javac method identity; only attributed non-error types qualify. */
    private static String bindingId(ExecutableElement executable, Elements elements) {
        Element parent = executable.getEnclosingElement();
        if (!(parent instanceof TypeElement)) return "";
        String owner = elements.getBinaryName((TypeElement)parent).toString();
        if (owner.isEmpty() || parent.asType().getKind() == TypeKind.ERROR) return "";
        List<String> params = new ArrayList<String>();
        for (VariableElement param : executable.getParameters()) {
            TypeMirror type = param.asType();
            if (type.getKind() == TypeKind.ERROR || type.getKind() == TypeKind.NONE
                    || type.toString().contains("<error>")) return "";
            params.add(type.toString());
        }
        return owner + "#" + executable.getSimpleName().toString()
            + "(" + String.join(",", params) + ")";
    }

    private static final class Source extends SimpleJavaFileObject {
        final String text;
        Source(Path file, String text) {
            super(file.toUri(), Kind.SOURCE);
            this.text = text;
        }
        @Override public CharSequence getCharContent(boolean ignored) { return text; }
    }

    private static final class Scanner extends TreePathScanner<Void, Void> {
        final CompilationUnitTree unit;
        final SourcePositions positions;
        final Trees trees;
        final Elements elements;
        final String path;
        final String pkg;
        final Deque<String> classStack = new ArrayDeque<String>();
        final Deque<Map<String, String>> fieldTypes = new ArrayDeque<Map<String, String>>();
        Map<String, String> localTypes = new HashMap<String, String>();
        String owner = null;

        Scanner(CompilationUnitTree unit, Trees trees, Elements elements, String path) {
            this.unit = unit;
            this.positions = trees.getSourcePositions();
            this.trees = trees;
            this.elements = elements;
            this.path = path;
            this.pkg = unit.getPackageName() == null ? "" : unit.getPackageName().toString();
        }

        int line(long offset) {
            return offset < 0 ? 0 : (int)unit.getLineMap().getLineNumber(offset);
        }

        String clazz() {
            List<String> names = new ArrayList<String>(classStack);
            Collections.reverse(names);
            return (pkg.isEmpty() ? "" : pkg + ".") + String.join("$", names);
        }

        @Override public Void visitClass(ClassTree tree, Void unused) {
            if (tree.getSimpleName().length() == 0) return super.visitClass(tree, unused);
            classStack.push(tree.getSimpleName().toString());
            Map<String, String> types = new HashMap<String, String>();
            for (Tree member : tree.getMembers()) if (member instanceof VariableTree) {
                VariableTree f = (VariableTree)member;
                types.put(f.getName().toString(), f.getType() == null ? "" : f.getType().toString());
            }
            fieldTypes.push(types);
            super.visitClass(tree, unused);
            fieldTypes.pop();
            classStack.pop();
            return null;
        }

        @Override public Void visitMethod(MethodTree tree, Void unused) {
            if (classStack.isEmpty()) return super.visitMethod(tree, unused);
            StringBuilder signature = new StringBuilder(tree.getName().toString()).append('(');
            int count = 0;
            Map<String, String> scoped = new HashMap<String, String>(fieldTypes.peek());
            for (VariableTree p : tree.getParameters()) {
                if (count++ > 0) signature.append(',');
                signature.append(p.getType() == null ? "?" : p.getType().toString());
                scoped.put(p.getName().toString(), p.getType() == null ? "" : p.getType().toString());
            }
            signature.append(')');
            String previousOwner = owner;
            Map<String, String> previousTypes = localTypes;
            owner = clazz() + "#" + signature.toString();
            localTypes = scoped;
            long start = positions.getStartPosition(unit, tree);
            long end = positions.getEndPosition(unit, tree);
            Map<String, String> attrs = fields("path", path, "symbol", owner, "class", clazz(),
                "name", tree.getName().toString(), "signature", signature.toString(),
                "arity", String.valueOf(count));
            Element methodElement = trees.getElement(getCurrentPath());
            String methodBinding = methodElement instanceof ExecutableElement
                ? bindingId((ExecutableElement)methodElement, elements) : "";
            attrs.put("binding_id", methodBinding);
            attrs.put("binding_status", methodBinding.isEmpty()
                ? "ATTRIBUTION_UNAVAILABLE" : "JAVAC_ATTRIBUTED");
            emit("method", attrs, ints("start", line(start), "end", line(Math.max(start, end - 1))));
            super.visitMethod(tree, unused);
            owner = previousOwner;
            localTypes = previousTypes;
            return null;
        }

        @Override public Void visitVariable(VariableTree tree, Void unused) {
            if (owner != null && tree.getType() != null)
                localTypes.put(tree.getName().toString(), tree.getType().toString());
            return super.visitVariable(tree, unused);
        }

        @Override public Void visitMethodInvocation(MethodInvocationTree tree, Void unused) {
            if (owner != null) {
                ExpressionTree select = tree.getMethodSelect();
                String receiver = "";
                String name = select.toString();
                if (select instanceof MemberSelectTree) {
                    MemberSelectTree m = (MemberSelectTree)select;
                    name = m.getIdentifier().toString();
                    receiver = m.getExpression().toString();
                }
                String type = receiver.equals("this") || receiver.equals("super") ? clazz() : localTypes.get(receiver);
                if (type == null && receiver.length() > 0 && Character.isUpperCase(receiver.charAt(0)))
                    type = receiver;
                Element resolved = trees.getElement(getCurrentPath());
                String targetId = resolved instanceof ExecutableElement
                    ? bindingId((ExecutableElement)resolved, elements) : "";
                Map<String, String> attrs = fields(
                    "path", path, "owner", owner, "name", name,
                    "receiver", receiver, "receiver_type", type == null ? "" : type,
                    "target_binding_id", targetId,
                    "binding_status", targetId.isEmpty()
                        ? "ATTRIBUTION_UNAVAILABLE" : "JAVAC_ATTRIBUTED");
                emit("call", attrs, ints("arity", tree.getArguments().size(), "line",
                                        line(positions.getStartPosition(unit, tree))));
            }
            return super.visitMethodInvocation(tree, unused);
        }
    }

    public static void main(String[] args) throws Exception {
        if (args.length != 1) {
            System.err.println("Usage: JavaAstScanner <source-root>");
            System.exit(2);
        }
        final Path root = Paths.get(args[0]).toAbsolutePath().normalize();
        JavaCompiler compiler = ToolProvider.getSystemJavaCompiler();
        if (compiler == null) throw new IllegalStateException("JDK required (not JRE)");
        try (java.util.stream.Stream<Path> stream = Files.walk(root)) {
            for (Path file : (Iterable<Path>)stream.filter(
                    p -> Files.isRegularFile(p) && p.toString().endsWith(".java"))::iterator) {
                String rel = root.relativize(file).toString().replace('\\', '/');
                try {
                    String source = new String(Files.readAllBytes(file), StandardCharsets.UTF_8);
                    StandardJavaFileManager manager = compiler.getStandardFileManager(
                        null, null, StandardCharsets.UTF_8);
                    try {
                        DiagnosticCollector<JavaFileObject> diagnostics =
                            new DiagnosticCollector<JavaFileObject>();
                        JavacTask task = (JavacTask)compiler.getTask(new StringWriter(),
                            manager, diagnostics,
                            Arrays.asList("-proc:none", "-implicit:none",
                                "-sourcepath", root.toString()), null,
                            Arrays.asList(new Source(file, source)));
                        Iterable<? extends CompilationUnitTree> units = task.parse();
                        try {
                            // Symbol attribution only. No generate(), no project build,
                            // no annotation processors or script execution.
                            task.analyze();
                        } catch (RuntimeException attributionFailure) {
                            // Keep syntax-only evidence when external classpath is missing.
                        }
                        Trees trees = Trees.instance(task);
                        for (CompilationUnitTree unit : units)
                            new Scanner(unit, trees, task.getElements(), rel).scan(unit, null);
                    } finally {
                        manager.close();
                    }
                } catch (Exception ex) {
                    emit("error", fields("path", rel, "error",
                        ex.getClass().getSimpleName() + ": " + ex.getMessage()),
                        new LinkedHashMap<String, Integer>());
                }
            }
        }
    }
}
