import com.sun.source.tree.*;
import com.sun.source.util.*;
import javax.tools.*;
import javax.lang.model.element.*;
import javax.lang.model.type.*;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;

/** JDK 8 SDK use attribution. Parse/analyze only; never generate or compile user classes. */
public final class SdkBindingScanner {
    static String quote(String s) {
        if (s == null) return "null";
        StringBuilder b = new StringBuilder("\"");
        for (char c : s.toCharArray()) {
            if (c == '"' || c == '\\') b.append('\\').append(c);
            else if (c == '\n') b.append("\\n");
            else if (c == '\r') b.append("\\r");
            else if (c == '\t') b.append("\\t");
            else if (c < 32) b.append(String.format("\\u%04x", (int)c));
            else b.append(c);
        }
        return b.append('"').toString();
    }
    static void report(String kind, String path, int line, String owner,
                       String member, String memberKind, String params, String detail) {
        System.out.println("{\"kind\":" + quote(kind) +
            ",\"path\":" + quote(path) + ",\"line\":" + line +
            ",\"owner\":" + quote(owner) + ",\"member\":" + quote(member) +
            ",\"member_kind\":" + quote(memberKind) +
            ",\"parameters\":" + quote(params) +
            ",\"detail\":" + quote(detail) + "}");
    }
    static final class Reader extends TreePathScanner<Void, Void> {
        final Trees trees;
        final CompilationUnitTree unit;
        final String path;
        final Set<String> sdkTypes;
        final boolean provider;
        Reader(Trees t, CompilationUnitTree u, String p, String sdk, boolean isProvider) {
            trees = t; unit = u; path = p;
            sdkTypes = new HashSet<String>(Arrays.asList(sdk.split(",")));
            provider = isProvider;
        }
        void record(Tree tree, Element symbol, String kind) {
            if (symbol == null || (provider && !"DECLARATION".equals(kind))) return;
            if (symbol.getKind() != ElementKind.METHOD &&
                symbol.getKind() != ElementKind.FIELD &&
                symbol.getKind() != ElementKind.ENUM_CONSTANT) return;
            Element type = symbol.getEnclosingElement();
            if (!(type instanceof TypeElement)) return;
            String owner = ((TypeElement)type).getQualifiedName().toString();
            if (!sdkTypes.contains(owner)) return;
            if (type.asType().getKind() == TypeKind.ERROR ||
                symbol.asType().getKind() == TypeKind.ERROR) return;
            String methodParams = "";
            if (symbol instanceof ExecutableElement) {
                List<String> types = new ArrayList<String>();
                for (VariableElement arg : ((ExecutableElement)symbol).getParameters()) {
                    if (arg.asType().getKind() == TypeKind.ERROR) return;
                    types.add(arg.asType().toString());
                }
                methodParams = String.join(",", types);
            }
            long offset = trees.getSourcePositions().getStartPosition(unit, tree);
            if (offset < 0) return;
            report(provider ? "declaration" : "binding", path,
                (int)unit.getLineMap().getLineNumber(offset),
                owner, symbol.getSimpleName().toString(), symbol.getKind().name(),
                methodParams, kind);
        }
        @Override public Void visitMethod(MethodTree tree, Void arg) {
            if (provider) record(tree, trees.getElement(getCurrentPath()), "DECLARATION");
            return super.visitMethod(tree, arg);
        }
        @Override public Void visitVariable(VariableTree tree, Void arg) {
            if (provider) record(tree, trees.getElement(getCurrentPath()), "DECLARATION");
            return super.visitVariable(tree, arg);
        }
        @Override public Void visitMethodInvocation(MethodInvocationTree tree, Void arg) {
            // One event per invocation: never duplicate method-select traversal.
            record(tree, trees.getElement(getCurrentPath()), "CALL");
            scan(tree.getTypeArguments(), arg);
            scan(tree.getArguments(), arg);
            return null;
        }
        @Override public Void visitMemberSelect(MemberSelectTree tree, Void arg) {
            record(tree, trees.getElement(getCurrentPath()), "SELECT");
            return super.visitMemberSelect(tree, arg);
        }
        @Override public Void visitIdentifier(IdentifierTree tree, Void arg) {
            record(tree, trees.getElement(getCurrentPath()), "IDENTIFIER");
            return null;
        }
    }
    static List<File> files(Path root) throws IOException {
        List<File> out = new ArrayList<File>();
        try (java.util.stream.Stream<Path> walk = Files.walk(root)) {
            walk.filter(p -> Files.isRegularFile(p) && p.toString().endsWith(".java"))
                .forEach(p -> out.add(p.toFile()));
        }
        Collections.sort(out, (a, b) -> a.getPath().compareTo(b.getPath()));
        return out;
    }
    public static void main(String[] argv) throws Exception {
        if (argv.length != 3) throw new IllegalArgumentException(
            "SdkBindingScanner <sdk-source-root> <consumer-source-root> <sdk-package-prefix>");
        Path sdk = Paths.get(argv[0]).toAbsolutePath().normalize();
        Path consumer = Paths.get(argv[1]).toAbsolutePath().normalize();
        String sdkPrefix = argv[2];
        JavaCompiler compiler = ToolProvider.getSystemJavaCompiler();
        if (compiler == null) throw new IllegalStateException("full JDK 8 required");
        List<File> all = files(sdk);
        List<File> users = files(consumer);
        if (all.isEmpty() || users.isEmpty()) {
            report("gap", "", 0, "", "", "", "", "NO_SOURCE_TO_ATTRIBUTE");
            return;
        }
        if (all.size() + users.size() > 2500)
            throw new IllegalArgumentException("AST source file budget exceeded");
        all.addAll(users);
        DiagnosticCollector<JavaFileObject> errors = new DiagnosticCollector<JavaFileObject>();
        StandardJavaFileManager manager = compiler.getStandardFileManager(errors, null,
                                                                           StandardCharsets.UTF_8);
        try {
            Iterable<? extends JavaFileObject> src = manager.getJavaFileObjectsFromFiles(all);
            List<String> options = Arrays.asList("-proc:none", "-implicit:none",
                "-source", "8", "-target", "8", "-sourcepath", sdk.toString());
            JavacTask task = (JavacTask)compiler.getTask(new StringWriter(), manager,
                                                          errors, options, null, src);
            Iterable<? extends CompilationUnitTree> parsed = task.parse();
            List<CompilationUnitTree> units = new ArrayList<CompilationUnitTree>();
            for (CompilationUnitTree u : parsed) units.add(u);
            try { task.analyze(); }
            catch (RuntimeException ex) {
                report("gap", "", 0, "", "", "", "", "ATTRIBUTION_FAILED:" + ex.getClass().getSimpleName());
            }
            Trees trees = Trees.instance(task);
            for (CompilationUnitTree unit : units) {
                Path path = Paths.get(unit.getSourceFile().toUri()).toAbsolutePath().normalize();
                boolean isProvider = path.startsWith(sdk);
                boolean isConsumer = path.startsWith(consumer);
                if (!isProvider && !isConsumer) continue;
                String relative = (isProvider ? sdk : consumer).relativize(path)
                    .toString().replace('\\', '/');
                new Reader(trees, unit, relative, sdkPrefix, isProvider).scan(unit, null);
            }
            int displayed = 0;
            for (Diagnostic<? extends JavaFileObject> error : errors.getDiagnostics()) {
                if (error.getKind() != Diagnostic.Kind.ERROR) continue;
                if (displayed++ >= 30) {
                    report("gap", "", 0, "", "", "", "", "DIAGNOSTIC_LIMIT");
                    break;
                }
                report("gap", "", (int)Math.max(0, error.getLineNumber()), "",
                    "", "", "", "JAVAC_ERROR:" + error.getCode());
            }
        } finally { manager.close(); }
    }
}